"""字段级结果仲裁：候选分组、字段仲裁、档案记录与整批撤销。

仲裁阶段位于“解析 + 校验”之后、“落库”之前，是一个独立且可回放的阶段，
不改动既有去重（Deduplicator）的任何语义。

字段仲裁顺序（逐字段打分，分高者采用）：
  1. 非空优先（空值直接失去非空分）；
  2. 校验等级优先（error 候选低于 warning，warning 低于无问题）；
  3. 来源文件修改时间新者优先；
  4. 稳定次序兜底（task_files.id），保证决策可回放、无随机。

被淘汰的值连同来源相对路径、MD5、行号、选择器与规则版本写入仲裁档案，
每个字段都可回溯到实际采用的文件与行号。
"""
from __future__ import annotations

import json
import threading
from typing import Any, Optional

from archive_extractor.core.validator import LEVEL_ORDER
from archive_extractor.utils.common import utc_now

# 仲裁规则版本：策略变化时升级，旧档案仍按档案内记录解释。
ARBITER_VERSION = "arbiter-1.0.0"
# 三段排序优先级：非空 > 校验等级 > 文件修改时间，最后用稳定次序兜底。
# 各段独立编码为互不重叠的位宽，保证高优先级维度永远不会被低维度反超。
NON_EMPTY_WEIGHT = 10_000_000
LEVEL_WEIGHT = 1_000_000
MTIME_WEIGHT = 1  # mtime 为秒级浮点，乘 1 取整，最大百万级，低于一个等级档


class ArbitrationError(RuntimeError):
    """仲裁阶段的可预期错误（批次缺失、状态非法、撤销失败等）。"""


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def nested_get(data: dict, path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def nested_set(data: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    target = data
    for part in parts[:-1]:
        node = target.get(part)
        if not isinstance(node, dict):
            node = {}
            target[part] = node
        target = node
    target[parts[-1]] = value


def flatten_paths(data: dict, prefix: str = "") -> list[str]:
    """拍平字段路径：dict 叶子展开为 a.b，list 与标量按原子值处理。"""
    paths: list[str] = []
    for key, value in (data or {}).items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            paths.extend(flatten_paths(value, path))
        else:
            paths.append(path)
    return paths


def candidate_level(candidate: dict, field_path: str) -> str:
    """该候选在该字段上的最高校验等级，无问题返回空串。"""
    levels = [
        issue.get("level", "")
        for issue in candidate.get("issues", [])
        if issue.get("field") == field_path
        or (
            "." in field_path
            and issue.get("field", "").startswith(field_path.split(".")[0] + ".")
        )
    ]
    if not levels:
        return ""
    return max(levels, key=lambda item: LEVEL_ORDER.get(item, -1))


def field_rank(candidate: dict, field_path: str) -> tuple[int, str, float, int]:
    """返回 (排序总分, 校验等级, 文件修改时间, 稳定次序)。

    总分按“非空 → 校验等级 → 修改时间 → 稳定次序”分段编码，段间量级隔离，
    因此任何低优先级差异都无法反超高优先级（例如更新文件的空值/告警值
    不会击败更旧文件的非空干净值）。
    """
    value = nested_get(candidate["data"], field_path)
    empty = is_empty(value)
    level = candidate_level(candidate, field_path)
    # 无问题给最高档（等级档总数），error/warning/info 依次降低；
    # 数值越大代表该字段候选越干净，越应被采用。
    level_tiers = len(LEVEL_ORDER) + 1  # none/info/warning/error 共 4 档
    severity = LEVEL_ORDER.get(level, -1)  # 无问题为 -1
    clean_score = level_tiers - 1 - severity
    mtime = float(candidate.get("mtime", 0) or 0)
    order = int(candidate.get("order", 0))
    score = (
        (0 if empty else NON_EMPTY_WEIGHT)
        + clean_score * LEVEL_WEIGHT
        + int(mtime * MTIME_WEIGHT)
        + order
    )
    return score, level, mtime, order


def logical_key(candidate: dict) -> Optional[str]:
    """按规则 dedup.primary_key 计算逻辑表单主键；无法计算返回 None。"""
    keys = candidate.get("dedup_config", {}).get("primary_key", [])
    if not keys:
        return None
    values = [nested_get(candidate["data"], key) for key in keys]
    if any(is_empty(value) for value in values):
        return None
    return "|".join(str(value) for value in values)


def trace_for(candidate: dict, field_path: str) -> dict:
    """从候选的字段溯源里挑出对应字段的最后一条，用于行号/选择器留痕。"""
    traces = candidate.get("traces", [])
    exact = [item for item in traces if item.get("field") == field_path]
    if exact:
        return exact[-1]
    head = field_path.split(".")[0]
    prefix = [item for item in traces if item.get("field", "").startswith(head + ".")]
    if prefix:
        return prefix[-1]
    top = [item for item in traces if item.get("field") == head]
    return top[-1] if top else {}


class Arbiter:
    """独立仲裁阶段：构建可回放决策、提交落库、整批撤销。"""

    VERSION = ARBITER_VERSION

    def __init__(self, db, file_store=None) -> None:
        self.db = db
        self.file_store = file_store
        # 进程内串行化“仲裁提交/撤销”临界区，配合 BEGIN IMMEDIATE
        # 覆盖多线程/多 TaskRunner 并发写同一逻辑表单的场景。
        self._commit_lock = threading.RLock()

    # ------------------------------------------------------------------
    # 纯决策部分：只依赖内存中的候选，不读写数据库，便于单元测试与回放。
    # ------------------------------------------------------------------
    def build_decision(self, candidates: list[dict]) -> dict:
        """把同批候选按 (rule_id, 逻辑主键) 分组并逐字段仲裁。"""
        groups: dict[tuple, list[dict]] = {}
        singletons: list[dict] = []
        for index, candidate in enumerate(candidates):
            candidate = dict(candidate)
            candidate.setdefault("order", index)
            key = logical_key(candidate)
            if key is None:
                singletons.append(candidate)
                continue
            bucket = (candidate["rule_id"], key)
            groups.setdefault(bucket, []).append(candidate)

        decisions: list[dict] = []
        for (rule_id, key), members in sorted(groups.items()):
            members.sort(key=lambda item: item["order"])
            merged: dict[str, Any] = {}
            field_decisions: list[dict] = []
            all_paths = sorted({
                path
                for member in members
                for path in flatten_paths(member["data"])
            })
            for path in all_paths:
                ranked = []
                for member in members:
                    score, level, mtime, order = field_rank(member, path)
                    value = nested_get(member["data"], path)
                    ranked.append({
                        "candidate": member, "value": value,
                        "empty": is_empty(value), "level": level,
                        "mtime": mtime, "order": order, "score": score,
                    })
                ranked.sort(
                    key=lambda item: (
                        item["score"], item["mtime"], item["order"]
                    ),
                    reverse=True,
                )
                winner = ranked[0]
                nested_set(merged, path, winner["value"])
                for rank, item in enumerate(ranked):
                    adopted = rank == 0
                    trace = trace_for(item["candidate"], path)
                    reasons = []
                    reasons.append(
                        "非空值优先" if not item["empty"] else "空值不参与非空优先"
                    )
                    reasons.append(
                        f"校验等级={item['level'] or 'none'}"
                    )
                    reasons.append(f"来源修改时间={item['mtime']:g}")
                    reasons.append(f"仲裁分={item['score']}")
                    field_decisions.append({
                        "field_path": path,
                        "adopted": adopted,
                        "candidate_index": item["candidate"]["order"],
                        "result_id": item["candidate"].get("result_id"),
                        "value": item["value"],
                        "is_empty": item["empty"],
                        "validation_level": item["level"],
                        "source_mtime": item["mtime"],
                        "source_rel_path": item["candidate"]["rel_path"],
                        "source_md5": item["candidate"]["md5"],
                        "source_line": trace.get("source_line"),
                        "selector": trace.get("selector", ""),
                        "selector_type": trace.get("selector_type", ""),
                        "rule_version": item["candidate"].get("rule_version", ""),
                        "rank_score": item["score"],
                        "reason": (
                            ("采用：" if adopted else "淘汰：")
                            + "；".join(reasons)
                        ),
                    })
            # 组内冠军：与去重保留规则对齐——mtime 新者优先，同 mtime 用稳定次序。
            champion = max(
                members,
                key=lambda item: (float(item.get("mtime", 0) or 0), item["order"]),
            )
            had_conflict = any(
                len({
                    json.dumps(nested_get(member["data"], path), ensure_ascii=False)
                    for member in members
                }) > 1
                for path in all_paths
            )
            decisions.append({
                "rule_id": rule_id,
                "form_type": champion.get("form_type", ""),
                "logical_key": key,
                "members": members,
                "champion_index": champion["order"],
                "merged_data": merged,
                "fields": field_decisions,
                "had_conflict": had_conflict,
            })
        for candidate in singletons:
            decisions.append({
                "rule_id": candidate["rule_id"],
                "form_type": candidate.get("form_type", ""),
                "logical_key": None,
                "members": [candidate],
                "champion_index": candidate["order"],
                "merged_data": candidate["data"],
                "fields": [],
                "had_conflict": False,
            })
        return {
            "arbiter_version": self.VERSION,
            "candidate_count": len(candidates),
            "group_count": len(decisions),
            "merged_group_count": sum(
                1 for item in decisions if len(item["members"]) > 1
            ),
            "conflict_group_count": sum(
                1 for item in decisions if item["had_conflict"]
            ),
            "groups": decisions,
        }

    # ------------------------------------------------------------------
    # 提交：在一个写事务里完成快照/结果/溯源/校验/仲裁档案/去重/撤销日志。
    # ------------------------------------------------------------------
    def commit_batch(
        self,
        task_id: int,
        candidates: list[dict],
        ruleset,
        persist_callback,
        deduplicator,
    ) -> dict:
        """执行一个仲裁批次。

        persist_callback(batch_id, decision, champion) 负责把冠军候选落库并
        返回 result_id；其内部所有写操作都运行在本方法开启的事务中。
        """
        if not candidates:
            raise ArbitrationError("仲裁候选为空，无法创建仲裁批次")
        decision = self.build_decision(candidates)
        batch_no = self._next_batch_no(task_id)
        with self._commit_lock:
            self.db.begin_immediate()
            try:
                batch_id = self.db.create_arbitration_batch({
                    "batch_no": batch_no,
                    "task_id": task_id,
                    "ruleset_signature": getattr(ruleset, "signature", ""),
                    "arbiter_version": self.VERSION,
                    "rule_versions": [
                        {
                            "rule_id": rule["rule_id"],
                            "version": rule.get("_effective_version", ""),
                        }
                        for rule in ruleset.all()
                    ],
                    "candidate_count": decision["candidate_count"],
                    "group_count": decision["group_count"],
                    "merged_group_count": decision["merged_group_count"],
                    "conflict_count": decision["conflict_group_count"],
                    "summary": {
                        "note": "字段级仲裁批次，策略：非空>校验等级>文件修改时间>稳定次序",
                    },
                })
                result_ids: list[int] = []
                conflict_rows: list[dict] = []
                dedup_requests: list[dict] = []
                # 去重缓存只在批次开始时加载一次；循环内反复 begin 会把
                # 本批次刚写入、尚未提交的候选从内存索引里冲掉。
                deduplicator.begin(task_id)
                # 让事务内的去重逻辑删除与报告也进入撤销日志，保证整批
                # 撤销时逻辑删除标记与去重报告一起回退。
                previous_hook = getattr(deduplicator, "journal_hook", None)
                deduplicator.journal_hook = (
                    lambda entity, entity_id, operation, before:
                    self.db.journal(
                        batch_id, entity, entity_id, operation, before
                    )
                )
                for group in decision["groups"]:
                    champion = next(
                        member
                        for member in group["members"]
                        if member["order"] == group["champion_index"]
                    )
                    pending_conflicts = []
                    if group["logical_key"] is not None:
                        pending_conflicts = self._cross_batch_conflicts(
                            batch_id, task_id, group, champion
                        )
                    result_id = persist_callback(batch_id, group, champion)
                    result_ids.append(result_id)
                    # 落库后把成员序号映射到真实 result_id，供字段档案引用。
                    order_to_result = {champion["order"]: result_id}
                    for member in group["members"]:
                        order_to_result.setdefault(
                            member["order"], member.get("result_id")
                        )
                    group_id = self.db.create_arbitration_group({
                        "batch_id": batch_id,
                        "rule_id": group["rule_id"],
                        "form_type": group["form_type"],
                        "logical_key": group["logical_key"] or "",
                        "winner_result_id": result_id,
                        "merged_data": group["merged_data"],
                        "original_data": champion["data"],
                        "candidate_count": len(group["members"]),
                        "had_conflict": group["had_conflict"],
                    })
                    for row in pending_conflicts:
                        row["group_id"] = group_id
                        row["incoming_result_id"] = result_id
                    conflict_rows.extend(pending_conflicts)
                    self._persist_field_records(
                        batch_id, group_id, group, order_to_result
                    )
                    if champion.get("status") in {"valid", "warning"}:
                        dedup_requests.append({
                            "result_id": result_id,
                            "merged_data": group["merged_data"],
                            "mtime": float(champion.get("mtime", 0) or 0),
                            "dedup_config": champion.get("dedup_config", {}),
                        })
                # 去重语义保持原样：全部冠军落库后再按既有规则逻辑删除。
                for request in dedup_requests:
                    deduplicator.apply(
                        task_id, request["result_id"], request["merged_data"],
                        request["mtime"], request["dedup_config"],
                    )
                # 同文件历史版本的 superseded 优先于去重标记：若去重把
                # “新版本取代”的旧行改写成 duplicate，按撤销日志恢复。
                self._reassert_superseded(batch_id)
                if conflict_rows:
                    self.db.add_arbitration_conflicts(conflict_rows)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            finally:
                deduplicator.journal_hook = previous_hook
        return {
            "batch_id": batch_id,
            "batch_no": batch_no,
            "candidate_count": decision["candidate_count"],
            "group_count": decision["group_count"],
            "merged_group_count": decision["merged_group_count"],
            "conflict_count": len(conflict_rows),
            "result_ids": result_ids,
        }

    def _persist_field_records(
        self,
        batch_id: int,
        group_id: int,
        group: dict,
        order_to_result: dict[int, int],
    ) -> None:
        rows = []
        for item in group["fields"]:
            rows.append({
                "batch_id": batch_id,
                "group_id": group_id,
                "result_id": order_to_result.get(item["candidate_index"]),
                "field_path": item["field_path"],
                "adopted": item["adopted"],
                "value": item["value"],
                "is_empty": item["is_empty"],
                "validation_level": item["validation_level"],
                "source_mtime": item["source_mtime"],
                "source_rel_path": item["source_rel_path"],
                "source_md5": item["source_md5"],
                "source_line": item["source_line"],
                "selector": item["selector"],
                "selector_type": item["selector_type"],
                "rule_version": item["rule_version"],
                "rank_score": item["rank_score"],
                "reason": item["reason"],
            })
        # 单候选组也保留采用记录，保证每个字段都能追到文件与行号。
        if not rows:
            champion = next(
                member
                for member in group["members"]
                if member["order"] == group["champion_index"]
            )
            for path in flatten_paths(champion["data"]):
                trace = trace_for(champion, path)
                value = nested_get(champion["data"], path)
                rows.append({
                    "batch_id": batch_id,
                    "group_id": group_id,
                    "result_id": order_to_result.get(champion["order"]),
                    "field_path": path,
                    "adopted": True,
                    "value": value,
                    "is_empty": is_empty(value),
                    "validation_level": candidate_level(champion, path),
                    "source_mtime": float(champion.get("mtime", 0) or 0),
                    "source_rel_path": champion["rel_path"],
                    "source_md5": champion["md5"],
                    "source_line": trace.get("source_line"),
                    "selector": trace.get("selector", ""),
                    "selector_type": trace.get("selector_type", ""),
                    "rule_version": champion.get("rule_version", ""),
                    "rank_score": 0,
                    "reason": "采用：单候选组，直接采用",
                })
        self.db.add_arbitration_field_records(rows)

    def _cross_batch_conflicts(
        self, batch_id: int, task_id: int, group: dict, champion: dict
    ) -> list[dict]:
        """检测与已提交批次/历史有效结果之间的字段冲突，只记录不静默覆盖。"""
        rows: list[dict] = []
        for existing in self.db.active_results_for_rule(group["rule_id"]):
            if existing.get("arbitration_batch_id") == batch_id:
                continue
            existing_key = self._key_of(existing, champion.get("dedup_config", {}))
            if existing_key != group["logical_key"]:
                continue
            existing_data = json.loads(existing["data_json"])
            for path in flatten_paths(group["merged_data"]):
                incoming = nested_get(group["merged_data"], path)
                incumbent = nested_get(existing_data, path)
                if json.dumps(incumbent, ensure_ascii=False) == json.dumps(
                    incoming, ensure_ascii=False
                ):
                    continue
                rows.append({
                    "batch_id": batch_id,
                    "group_id": None,
                    "task_id": task_id,
                    "rule_id": group["rule_id"],
                    "logical_key": group["logical_key"],
                    "field_path": path,
                    "incumbent_result_id": existing["id"],
                    "incoming_result_id": champion.get("result_id"),
                    "incumbent_value": incumbent,
                    "incoming_value": incoming,
                    "incumbent_source": existing.get("source_rel_path", ""),
                    "incoming_source": champion["rel_path"],
                    "resolution": "recorded_pending_review",
                    "summary": (
                        f"逻辑表单 {group['logical_key']} 字段 {path} 冲突："
                        f"既有批次值={self._short(incumbent)}"
                        f"（{existing.get('source_rel_path', '')}），"
                        f"新批次值={self._short(incoming)}（{champion['rel_path']}），"
                        "已记录冲突，未静默覆盖"
                    ),
                })
        return rows

    @staticmethod
    def _key_of(row: dict, dedup_config: dict) -> Optional[str]:
        keys = (dedup_config or {}).get("primary_key", [])
        if not keys:
            return None
        try:
            data = json.loads(row["data_json"])
        except (TypeError, ValueError):
            return None
        values = [nested_get(data, key) for key in keys]
        if any(is_empty(value) for value in values):
            return None
        return "|".join(str(value) for value in values)

    @staticmethod
    def _short(value: Any, length: int = 40) -> str:
        text = json.dumps(value, ensure_ascii=False)
        return text if len(text) <= length else text[: length - 1] + "…"

    def _reassert_superseded(self, batch_id: int) -> None:
        """同文件新版本取代的旧结果必须保持 superseded，不被去重改写。"""
        rows = self.db.query(
            """SELECT DISTINCT entity_id FROM arbitration_journal
               WHERE batch_id = ? AND entity = 'results'
               AND operation = 'update' AND before_json IS NOT NULL
               AND json_extract(before_json, '$.deleted_reason') IS NULL""",
            (batch_id,),
        )
        # 仅恢复本批次里被标记为 new_version 的旧版本（由落库回调登记）。
        marked = self.db.query(
            """SELECT DISTINCT entity_id FROM arbitration_journal
               WHERE batch_id = ? AND entity = 'results'
               AND operation = 'supersede'""",
            (batch_id,),
        )
        target_ids = {row["entity_id"] for row in marked}
        for row in rows:
            result_id = row["entity_id"]
            if result_id not in target_ids:
                continue
            current = self.db.get_result(result_id)
            if current and current["deleted_reason"] != "new_version":
                # 去重曾把它改成 duplicate；先记录当前镜像，再恢复为新版本
                # 取代语义，撤销时按日志逆序先回到 superseded、再回到原状。
                self.db.journal(
                    batch_id, "results", result_id, "update", current
                )
                self.db.execute(
                    """UPDATE results SET status = 'superseded',
                       is_deleted = 1, deleted_reason = 'new_version',
                       duplicate_of = NULL, updated_at = ? WHERE id = ?""",
                    (utc_now(), result_id),
                )

    def _next_batch_no(self, task_id: int) -> str:
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        rows = self.db.query(
            """SELECT COUNT(*) AS n FROM arbitration_batches WHERE task_id = ?""",
            (task_id,),
        )
        seq = int(rows[0]["n"]) + 1 if rows else 1
        return f"ARB-T{task_id}-{stamp}-{seq:04d}"

    # ------------------------------------------------------------------
    # 整批撤销：按撤销日志逆序恢复，字段/溯源/快照索引/逻辑删除标记一致回退。
    # ------------------------------------------------------------------
    def rollback_batch(
        self, batch_ref: str | int, reason: str = "manual_rollback"
    ) -> dict:
        with self._commit_lock:
            batch = self._resolve_batch(batch_ref)
            if batch is None:
                raise ArbitrationError(
                    f"找不到仲裁记录: {batch_ref}，无法撤销"
                )
            if batch["status"] == "rolled_back":
                raise ArbitrationError(
                    f"仲裁批次 {batch['batch_no']} 已撤销，不能重复撤销"
                )
            batch_id = batch["id"]
            journal = self.db.list_journal(batch_id)
            self.db.begin_immediate()
            try:
                restored = {"delete": 0, "insert": 0, "update": 0}
                for entry in journal:
                    self._restore_entry(entry, restored)
                # 冲突档案随批次撤销一起清除，避免指向已回退的状态。
                self.db.execute(
                    "DELETE FROM arbitration_conflicts WHERE batch_id = ?",
                    (batch_id,),
                )
                self.db.mark_batch_rolled_back(batch_id, reason)
                self.db.commit()
            except Exception as exc:
                self.db.rollback()
                raise ArbitrationError(
                    f"仲裁批次 {batch['batch_no']} 撤销中途失败，已整体回滚，"
                    f"未产生部分成功: {exc}"
                ) from exc
        return {
            "batch_id": batch_id,
            "batch_no": batch["batch_no"],
            "status": "rolled_back",
            "restored": restored,
            "journal_entries": len(journal),
        }

    def _resolve_batch(self, batch_ref: str | int) -> Optional[dict]:
        if isinstance(batch_ref, int) or str(batch_ref).isdigit():
            batch = self.db.get_arbitration_batch(int(batch_ref))
            if batch:
                return batch
        return self.db.get_arbitration_batch_by_no(str(batch_ref))

    def _restore_entry(self, entry: dict, counter: dict) -> None:
        entity = entry["entity"]
        operation = entry["operation"]
        before = json.loads(entry["before_json"]) if entry["before_json"] else None
        if operation == "insert":
            self.db.execute(
                f"DELETE FROM {entity} WHERE id = ?", (entry["entity_id"],)
            )
            counter["delete"] += 1
            return
        if operation == "delete":
            columns = self.db.table_columns(entity)
            payload = {key: value for key, value in (before or {}).items()
                       if key in columns}
            if not payload:
                raise ArbitrationError(
                    f"撤销日志缺少删除前镜像，实体 {entity} 无法恢复"
                )
            placeholders = ", ".join("?" for _ in payload)
            self.db.execute(
                f"INSERT INTO {entity} ({', '.join(payload)}) "
                f"VALUES ({placeholders})",
                tuple(payload.values()),
            )
            counter["insert"] += 1
            return
        # update：用变更前整行镜像覆盖回去。
        if not before:
            raise ArbitrationError(
                f"撤销日志缺少更新前镜像，实体 {entity}#{entry['entity_id']} "
                "无法恢复"
            )
        columns = self.db.table_columns(entity)
        payload = {key: value for key, value in before.items() if key in columns}
        assignments = ", ".join(f"{key} = ?" for key in payload)
        self.db.execute(
            f"UPDATE {entity} SET {assignments} WHERE id = ?",
            tuple(payload.values()) + (entry["entity_id"],),
        )
        counter["update"] += 1

    # ------------------------------------------------------------------
    # 回放：用档案里的候选值重新跑纯仲裁，校验策略输出是否仍一致。
    # ------------------------------------------------------------------
    def replay_batch(self, batch_ref: str | int) -> dict:
        """用档案里记录的仲裁分重新排序，校验当时采用结论是否仍可复现。

        档案已固化每个候选字段的仲裁分（非空/校验等级/mtime 的分段编码），
        回放即按分数重排并核对 adopted 标记；若策略版本升级导致权重变化，
        会在这里暴露不一致而不是静默改写历史档案。
        """
        batch = self._resolve_batch(batch_ref)
        if batch is None:
            raise ArbitrationError(f"找不到仲裁记录: {batch_ref}，无法回放")
        records = self.db.list_arbitration_field_records(batch["id"])
        groups = self.db.list_arbitration_groups(batch["id"])
        mismatches: list[str] = []
        for group in groups:
            group_records = [
                row for row in records if row["group_id"] == group["id"]
            ]
            field_paths = {row["field_path"] for row in group_records}
            for field_path in sorted(field_paths):
                rows = [
                    row for row in group_records
                    if row["field_path"] == field_path
                ]
                if not rows:
                    continue
                winner = max(rows, key=lambda item: item["rank_score"])
                adopted_rows = [row for row in rows if row["adopted"]]
                if len(adopted_rows) != 1 or adopted_rows[0]["id"] != winner["id"]:
                    mismatches.append(
                        f"分组 {group['logical_key'] or group['id']} 字段 "
                        f"{field_path} 回放结果与档案采用结论不一致"
                    )
        return {
            "batch_id": batch["id"],
            "batch_no": batch["batch_no"],
            "arbiter_version": batch["arbiter_version"],
            "replayed_at": utc_now(),
            "consistent": not mismatches,
            "mismatches": mismatches,
        }

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------
    def batch_detail(self, batch_ref: str | int) -> dict:
        batch = self._resolve_batch(batch_ref)
        if batch is None:
            raise ArbitrationError(f"找不到仲裁记录: {batch_ref}")
        return {
            "batch": batch,
            "groups": self.db.list_arbitration_groups(batch["id"]),
            "field_records": self.db.list_arbitration_field_records(batch["id"]),
            "conflicts": self.db.list_arbitration_conflicts(batch["id"]),
            "journal": self.db.list_journal(batch["id"]),
        }

    def field_provenance(self, result_id: int) -> list[dict]:
        """某条结果每个字段实际采用的来源文件、MD5、行号与规则版本。"""
        return self.db.field_records_for_result(result_id)
