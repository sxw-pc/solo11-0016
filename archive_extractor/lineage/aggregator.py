"""跨批次字段血缘聚合器（离线、只读、可复现）。

聚合口径（关键设计）：

1. 只读：仅调用 ``db.query`` / ``db.query_one`` 读取 tasks、results、traces、
   validations、rules_versions，不执行任何写库操作，也不触碰快照文件。
2. 字段归一：以「表单类型 + 点分字段路径」作为跨版本恒等键
   （canonical_key）。对象字段按 ``contact.email`` 形式扁平化；
   规则版本之间字段名不变即视为同一逻辑字段，字段重命名（无法建立恒等关系）
   走显式降级而不是静默合并。
3. 断点判定：逐批次比较相邻两个批次的选择器签名与值签名——
   - 选择器变化即规则断点；断点处值同时变化记为 ``rule_and_data``；
   - 选择器不变而值变化记为 ``data``（数据变化，不误判为规则变化）；
   - 任一侧缺少选择器证据（无 trace、字段缺失、批次无记录）时记为
     ``indeterminate`` 并带降级原因，绝不丢弃。
4. 可复现：所有产物为排序键的规范化 JSON，内容不含运行时间戳；
   文件名由批次集合与产物内容的哈希决定，同一组批次重复运行逐字节一致。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from archive_extractor.utils.common import ensure_dir

# 一次血缘聚合产出的全部中间/最终产物文件名（相对产物目录）。
LINEAGE_ARTIFACT_FILES = (
    "batch_manifest.json",
    "field_alignment.json",
    "lineage_view.json",
)
_RUN_INDEX_NAME = "runs_index.json"

# 对齐与断点结论取值。
ALIGN_STABLE = "aligned"
ALIGN_DEGRADED = "degraded"
TRANSITION_DATA = "data"
TRANSITION_RULE = "rule"
TRANSITION_RULE_AND_DATA = "rule_and_data"
TRANSITION_INDETERMINATE = "indeterminate"

# 降级原因码，便于程序消费与测试断言。
DEGRADE_NO_TRACE = "no_trace_evidence"
DEGRADE_FIELD_MISSING = "field_missing_in_batch"


def stable_dumps(obj: Any) -> str:
    """规范化 JSON 序列化：排序键、无多余空白、不转义非 ASCII。"""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def content_hash(obj: Any) -> str:
    """对任意可 JSON 化对象计算稳定 SHA256。"""
    return hashlib.sha256(stable_dumps(obj).encode("utf-8")).hexdigest()


def short_hash(obj: Any, length: int = 16) -> str:
    return content_hash(obj)[:length]


def lineage_run_id(task_ids: list[int]) -> str:
    """按批次集合生成确定性运行标识（与顺序无关，与集合内容有关）。"""
    return "lineage-" + short_hash(sorted(int(i) for i in task_ids))


def flatten_paths(value: Any, prefix: str = "") -> list[str]:
    """提取嵌套数据的全部点分字段路径（叶子路径与对象路径都保留）。"""
    paths: list[str] = []
    if isinstance(value, dict):
        for key in sorted(value):
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.append(path)
            paths.extend(flatten_paths(value[key], path))
    return paths


def get_path(value: Any, path: str) -> Any:
    """按点分路径取值，任一层缺失返回哨兵 ``_MISSING``。"""
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


_MISSING = object()


class LineageAggregator:
    """只读聚合多个任务批次，输出按表单类型+字段组织的血缘视图。"""

    def __init__(self, db, output_dir: Path | str) -> None:
        self.db = db
        self.output_dir = ensure_dir(Path(output_dir).expanduser().resolve())

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def run(self, task_ids: list[int]) -> dict:
        """执行聚合并落盘三份可复现产物，返回产物摘要。"""
        ids = sorted({int(i) for i in task_ids})
        if not ids:
            raise ValueError("至少需要指定一个任务批次")
        run_id = lineage_run_id(ids)
        run_dir = ensure_dir(self.output_dir / run_id)

        manifest = self._build_batch_manifest(ids)
        alignment = self._build_alignment(manifest)
        view = self._build_lineage_view(manifest, alignment)

        written = self._write_artifacts(run_dir, manifest, alignment, view)
        index = self._update_index(run_id, ids, written)
        return {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "task_ids": ids,
            "artifacts": written,
            "index": index,
            "form_types": view["summary"]["form_types"],
            "field_count": view["summary"]["field_count"],
            "breakpoint_count": view["summary"]["breakpoint_count"],
            "degraded_count": view["summary"]["degraded_count"],
        }

    def clean(self, run_id: Optional[str] = None) -> list[str]:
        """清理血缘产物。

        指定 run_id 只清理该次运行，否则清理整个产物目录。
        """
        removed: list[str] = []
        if run_id is None:
            target = self.output_dir
            if target.exists():
                for child in sorted(target.iterdir()):
                    removed.append(str(child))
                    if child.is_dir():
                        for file in sorted(child.rglob("*"), reverse=True):
                            if file.is_file():
                                file.unlink()
                        child.rmdir()
                    else:
                        child.unlink()
            return removed
        run_dir = (self.output_dir / run_id).resolve()
        if self.output_dir.resolve() not in run_dir.parents:
            raise ValueError("非法的运行标识")
        if run_dir.exists():
            for file in sorted(run_dir.rglob("*"), reverse=True):
                if file.is_file():
                    removed.append(str(file))
                    file.unlink()
            run_dir.rmdir()
            removed.append(str(run_dir))
        index_path = self.output_dir / _RUN_INDEX_NAME
        if index_path.exists():
            index = json.loads(index_path.read_text(encoding="utf-8"))
            runs = [item for item in index.get("runs", []) if item["run_id"] != run_id]
            if len(runs) != len(index.get("runs", [])):
                index["runs"] = runs
                index_path.write_text(stable_dumps(index), encoding="utf-8")
        return removed

    # ------------------------------------------------------------------
    # 阶段一：批次清单（只读采集，作为后续阶段的唯一输入）
    # ------------------------------------------------------------------
    def _build_batch_manifest(self, task_ids: list[int]) -> dict:
        batches = []
        for task_id in task_ids:
            task = self.db.query_one(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            )
            if task is None:
                raise ValueError(f"任务批次不存在: {task_id}")
            batches.append(self._collect_batch(task))
        # 时间轴排序：先按开始/创建时间，再按任务 id 兜底，保证确定性。
        batches.sort(key=lambda item: (
            item["task"].get("started_at") or item["task"].get("created_at") or "",
            item["task"]["id"],
        ))
        rule_versions = self._collect_rule_versions(task_ids)
        manifest = {
            "schema_version": 1,
            "ordering": [batch["task"]["id"] for batch in batches],
            "batches": batches,
            "rule_versions": rule_versions,
        }
        manifest["manifest_hash"] = short_hash(
            {k: v for k, v in manifest.items() if k != "manifest_hash"}
        )
        return manifest

    def _collect_batch(self, task: dict) -> dict:
        task_id = task["id"]
        results = self.db.query(
            "SELECT * FROM results WHERE task_id = ? ORDER BY id", (task_id,)
        )
        result_ids = [row["id"] for row in results]
        traces = self._collect_traces(result_ids)
        validations = self._collect_validations(result_ids)
        records = []
        for row in results:
            data = json.loads(row["data_json"] or "{}")
            records.append({
                "result_id": row["id"],
                "form_type": row.get("form_type") or "",
                "rule_id": row.get("rule_id") or "",
                "rule_version": row.get("rule_version") or "",
                "rule_content_hash": row.get("rule_content_hash") or "",
                "status": row.get("status") or "",
                "error_level": row.get("error_level") or "",
                "source_rel_path": row.get("source_rel_path") or "",
                "source_md5": row.get("source_md5") or "",
                "version": row.get("version") or 1,
                "is_deleted": int(row.get("is_deleted") or 0),
                "data": data,
                "field_paths": flatten_paths(data),
                "traces": traces.get(row["id"], []),
                "validations": validations.get(row["id"], []),
            })
        return {
            "task": {
                "id": task_id,
                "name": task.get("name") or "",
                "status": task.get("status") or "",
                "rules_dir": task.get("rules_dir") or "",
                "created_at": task.get("created_at") or "",
                "started_at": task.get("started_at") or "",
                "finished_at": task.get("finished_at") or "",
            },
            "result_count": len(records),
            "records": records,
        }

    def _collect_traces(self, result_ids: list[int]) -> dict[int, list[dict]]:
        if not result_ids:
            return {}
        placeholders = ",".join("?" for _ in result_ids)
        rows = self.db.query(
            f"""SELECT result_id, field, selector, selector_type, source_line,
                       raw_value, rule_version
                FROM traces WHERE result_id IN ({placeholders}) ORDER BY id""",
            tuple(result_ids),
        )
        grouped: dict[int, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["result_id"], []).append({
                "field": row["field"] or "",
                "selector": row["selector"] or "",
                "selector_type": row["selector_type"] or "",
                "source_line": row["source_line"],
                "raw_value": row["raw_value"] or "",
                "rule_version": row["rule_version"] or "",
            })
        return grouped

    def _collect_validations(self, result_ids: list[int]) -> dict[int, list[dict]]:
        if not result_ids:
            return {}
        placeholders = ",".join("?" for _ in result_ids)
        rows = self.db.query(
            f"""SELECT result_id, level, field, message, validator
                FROM validations WHERE result_id IN ({placeholders})
                ORDER BY id""",
            tuple(result_ids),
        )
        grouped: dict[int, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["result_id"], []).append({
                "level": row["level"] or "",
                "field": row["field"] or "",
                "message": row["message"] or "",
                "validator": row["validator"] or "",
            })
        return grouped

    def _collect_rule_versions(self, task_ids: list[int]) -> list[dict]:
        placeholders = ",".join("?" for _ in task_ids)
        rows = self.db.query(
            f"""SELECT rule_id, version, content_hash, source_path
                FROM rules_versions
                WHERE rule_id IN (
                    SELECT DISTINCT rule_id FROM results
                    WHERE task_id IN ({placeholders}) AND rule_id != ''
                )
                ORDER BY rule_id, version""",
            tuple(task_ids),
        )
        return [
            {
                "rule_id": row["rule_id"],
                "version": row["version"],
                "content_hash": row["content_hash"],
                "source_path": row["source_path"] or "",
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 阶段二：字段归一与跨版本对齐
    # ------------------------------------------------------------------
    def _build_alignment(self, manifest: dict) -> dict:
        fields: dict[str, dict] = {}
        for batch in manifest["batches"]:
            task_id = batch["task"]["id"]
            for record in batch["records"]:
                form_type = record["form_type"]
                trace_index = {
                    trace["field"]: trace for trace in record["traces"]
                }
                for path in sorted(set(record["field_paths"])):
                    key = self._canonical_key(form_type, path)
                    entry = fields.setdefault(key, {
                        "canonical_key": key,
                        "form_type": form_type,
                        "field_path": path,
                        "batch_evidence": {},
                    })
                    selector = self._selector_signature(trace_index.get(path))
                    entry["batch_evidence"][str(task_id)] = {
                        "selector_signature": selector,
                        "has_trace_evidence": selector is not None,
                        "rule_id": record["rule_id"],
                        "rule_version": record["rule_version"],
                    }
        # 对齐结论：按时间轴逐批次检查证据连续性。
        ordered_ids = [str(i) for i in manifest["ordering"]]
        alignment_entries = []
        for key in sorted(fields):
            entry = fields[key]
            evidence = entry["batch_evidence"]
            reasons = []
            for task_id in ordered_ids:
                if task_id not in evidence:
                    reasons.append(
                        f"{DEGRADE_FIELD_MISSING}:task={task_id}"
                    )
                elif not evidence[task_id]["has_trace_evidence"]:
                    reasons.append(
                        f"{DEGRADE_NO_TRACE}:task={task_id}"
                    )
            status = ALIGN_STABLE if not reasons else ALIGN_DEGRADED
            alignment_entries.append({
                "canonical_key": entry["canonical_key"],
                "form_type": entry["form_type"],
                "field_path": entry["field_path"],
                "alignment_status": status,
                "degrade_reasons": reasons,
                "batch_evidence": {
                    task_id: evidence[task_id] for task_id in ordered_ids
                    if task_id in evidence
                },
            })
        alignment = {
            "schema_version": 1,
            "strategy": (
                "canonical_key = form_type + '.' + flattened_field_path; "
                "selector identity derived from traces; missing evidence is "
                "explicitly degraded rather than dropped"
            ),
            "fields": alignment_entries,
        }
        alignment["alignment_hash"] = short_hash(
            {k: v for k, v in alignment.items() if k != "alignment_hash"}
        )
        return alignment

    @staticmethod
    def _canonical_key(form_type: str, field_path: str) -> str:
        return f"{form_type}.{field_path}"

    @staticmethod
    def _selector_signature(trace: Optional[dict]) -> Optional[dict]:
        if not trace:
            return None
        return {
            "selector": trace.get("selector") or "",
            "selector_type": trace.get("selector_type") or "",
        }

    # ------------------------------------------------------------------
    # 阶段三：时间轴血缘视图与断点判定
    # ------------------------------------------------------------------
    def _build_lineage_view(self, manifest: dict, alignment: dict) -> dict:
        alignment_index = {
            item["canonical_key"]: item for item in alignment["fields"]
        }
        # canonical_key -> 每批次的字段观测聚合。
        observed: dict[str, dict[str, dict]] = {}
        for batch in manifest["batches"]:
            task_id = str(batch["task"]["id"])
            for record in batch["records"]:
                for path, observation in self._record_observations(
                    record
                ).items():
                    key = self._canonical_key(record["form_type"], path)
                    observed.setdefault(key, {}).setdefault(
                        task_id,
                        {"items": [], "rule_versions": set(),
                         "selectors": [], "statuses": [], "levels": []},
                    )
                    bucket = observed[key][task_id]
                    bucket["items"].append(observation)
                    if record["rule_version"]:
                        bucket["rule_versions"].add(record["rule_version"])
                    if observation["selector"] is not None:
                        bucket["selectors"].append(observation["selector"])
                    bucket["statuses"].append(record["status"])
                    bucket["levels"].append(record["error_level"])

        field_views = []
        breakpoint_count = 0
        degraded_count = 0
        for key in sorted(observed):
            align = alignment_index.get(key)
            field_view = self._field_view(
                key, manifest["ordering"], observed[key], align
            )
            breakpoint_count += sum(
                1 for item in field_view["timeline"]
                if item.get("is_breakpoint")
            )
            if field_view["alignment_status"] == ALIGN_DEGRADED:
                degraded_count += 1
            field_views.append(field_view)

        form_types = sorted({view["form_type"] for view in field_views})
        view = {
            "schema_version": 1,
            "run_id": lineage_run_id(manifest["ordering"]),
            "batch_order": manifest["ordering"],
            "summary": {
                "batch_count": len(manifest["ordering"]),
                "form_types": form_types,
                "field_count": len(field_views),
                "breakpoint_count": breakpoint_count,
                "degraded_count": degraded_count,
            },
            "fields": field_views,
        }
        view["view_hash"] = short_hash(
            {k: v for k, v in view.items() if k != "view_hash"}
        )
        return view

    def _record_observations(self, record: dict) -> dict[str, dict]:
        """单条结果内每个字段路径的取值/选择器/校验结论。"""
        trace_index: dict[str, list[dict]] = {}
        for trace in record["traces"]:
            trace_index.setdefault(trace["field"], []).append(trace)
        validation_index: dict[str, list[dict]] = {}
        for issue in record["validations"]:
            validation_index.setdefault(issue["field"], []).append(issue)

        observations: dict[str, dict] = {}
        for path in sorted(set(record["field_paths"])):
            value = get_path(record["data"], path)
            field_traces = trace_index.get(path, [])
            selector = self._selector_signature(
                field_traces[0] if field_traces else None
            )
            raw_values = sorted({
                str(item.get("raw_value", "")) for item in field_traces
            })
            source_lines = sorted({
                int(item["source_line"]) for item in field_traces
                if item.get("source_line") is not None
            })
            field_validations = [
                {
                    "level": issue["level"],
                    "message": issue["message"],
                    "validator": issue["validator"],
                }
                for issue in validation_index.get(path, [])
            ]
            observations[path] = {
                "value": None if value is _MISSING else value,
                "value_present": value is not _MISSING,
                "selector": selector,
                "raw_values": raw_values,
                "source_lines": source_lines,
                "has_trace_evidence": bool(field_traces),
                "validations": field_validations,
                "validation_conclusion": self._conclusion(field_validations),
                "result_id": record["result_id"],
                "source_rel_path": record["source_rel_path"],
                "source_md5": record["source_md5"],
                "result_status": record["status"],
            }
        return observations

    @staticmethod
    def _conclusion(validations: list[dict]) -> str:
        levels = {item["level"] for item in validations}
        if "error" in levels:
            return "invalid"
        if "warning" in levels:
            return "warning"
        if "info" in levels:
            return "info"
        return "valid"

    def _field_view(
        self,
        key: str,
        ordering: list[int],
        buckets: dict[str, dict],
        align: Optional[dict],
    ) -> dict:
        _, _, field_path = key.partition(".")
        form_type = key[: len(key) - len(field_path) - 1]
        timeline = []
        previous: Optional[dict] = None
        for task_id in ordering:
            task_key = str(task_id)
            if task_key not in buckets:
                entry = {
                    "task_id": task_id,
                    "present": False,
                    "degraded": True,
                    "degrade_reason": DEGRADE_FIELD_MISSING,
                    "is_breakpoint": False,
                    "transition": None,
                }
                timeline.append(entry)
                previous = None
                continue
            entry = self._batch_field_entry(task_id, buckets[task_key])
            transition, reason = self._classify_transition(previous, entry)
            entry["transition"] = transition
            # 只有证据充分、确认发生选择器切换的位置才标规则断点；
            # 无法判定（缺 trace 证据）只降级，不误报为断点。
            entry["is_breakpoint"] = transition in {
                TRANSITION_RULE, TRANSITION_RULE_AND_DATA
            }
            entry["degraded"] = transition == TRANSITION_INDETERMINATE
            if reason:
                entry.setdefault("degrade_reasons", []).append(reason)
            timeline.append(entry)
            previous = entry
        return {
            "canonical_key": key,
            "form_type": form_type,
            "field_path": field_path,
            "alignment_status": (
                align["alignment_status"] if align else ALIGN_DEGRADED
            ),
            "degrade_reasons": (
                align["degrade_reasons"] if align else [DEGRADE_NO_TRACE]
            ),
            "timeline": timeline,
        }

    def _batch_field_entry(self, task_id: int, bucket: dict) -> dict:
        items = sorted(
            bucket["items"],
            key=lambda item: (item["source_rel_path"], item["result_id"]),
        )
        selectors = bucket["selectors"]
        selector = selectors[0] if selectors else None
        selector_consistent = all(
            stable_dumps(item) == stable_dumps(selector) for item in selectors
        )
        value_fingerprint = short_hash(
            [item["value"] for item in items], length=32
        )
        rule_versions = sorted(bucket["rule_versions"])
        return {
            "task_id": task_id,
            "present": True,
            "result_count": len(items),
            "rule_versions": rule_versions,
            "rule_version": rule_versions[0] if len(rule_versions) == 1 else None,
            "selector": selector,
            "selector_consistent": selector_consistent if selectors else False,
            "has_trace_evidence": bool(selectors),
            "value_fingerprint": value_fingerprint,
            "value_sample": items[0]["value"] if len(items) == 1 else None,
            "validation_summary": self._batch_validation(items),
            "result_ids": [item["result_id"] for item in items],
            "sources": [
                {
                    "result_id": item["result_id"],
                    "source_rel_path": item["source_rel_path"],
                    "source_md5": item["source_md5"],
                    "value": item["value"],
                    "raw_values": item["raw_values"],
                    "source_lines": item["source_lines"],
                    "selector": item["selector"],
                    "validation_conclusion": item["validation_conclusion"],
                    "validations": item["validations"],
                    "result_status": item["result_status"],
                }
                for item in items
            ],
        }

    @staticmethod
    def _batch_validation(items: list[dict]) -> dict:
        conclusions = sorted({item["validation_conclusion"] for item in items})
        highest = "valid"
        for level in ("invalid", "warning", "info"):
            if level in conclusions:
                highest = level
                break
        return {
            "conclusions": conclusions,
            "highest_conclusion": highest,
            "issue_count": sum(len(item["validations"]) for item in items),
        }

    def _classify_transition(
        self, previous: Optional[dict], current: dict
    ) -> tuple[Optional[str], Optional[str]]:
        """返回 (断点类型, 降级原因)。规则变化与数据变化在此严格区分。"""
        if previous is None:
            # 时间轴首批没有相邻批次可比较，不构成断点也不降级。
            return None, None
        if not previous.get("has_trace_evidence") or not current.get(
            "has_trace_evidence"
        ):
            return TRANSITION_INDETERMINATE, DEGRADE_NO_TRACE
        selector_changed = stable_dumps(
            previous.get("selector")
        ) != stable_dumps(current.get("selector"))
        value_changed = previous["value_fingerprint"] != current[
            "value_fingerprint"
        ]
        if selector_changed and value_changed:
            return TRANSITION_RULE_AND_DATA, None
        if selector_changed:
            return TRANSITION_RULE, None
        if value_changed:
            return TRANSITION_DATA, None
        return None, None

    # ------------------------------------------------------------------
    # 产物落盘（确定性写入 + 索引维护）
    # ------------------------------------------------------------------
    def _write_artifacts(
        self, run_dir: Path, manifest: dict, alignment: dict, view: dict
    ) -> dict[str, str]:
        payloads = {
            "batch_manifest.json": manifest,
            "field_alignment.json": alignment,
            "lineage_view.json": view,
        }
        written = {}
        for name in sorted(payloads):
            path = run_dir / name
            path.write_text(
                stable_dumps(payloads[name]), encoding="utf-8"
            )
            written[name] = short_hash(payloads[name], length=64)
        return written

    def _update_index(
        self, run_id: str, task_ids: list[int], written: dict[str, str]
    ) -> str:
        index_path = self.output_dir / _RUN_INDEX_NAME
        if index_path.exists():
            index = json.loads(index_path.read_text(encoding="utf-8"))
        else:
            index = {"schema_version": 1, "runs": []}
        index["runs"] = [
            item for item in index["runs"] if item["run_id"] != run_id
        ]
        index["runs"].append({
            "run_id": run_id,
            "task_ids": task_ids,
            "artifacts": written,
        })
        index["runs"].sort(key=lambda item: item["run_id"])
        index_path.write_text(stable_dumps(index), encoding="utf-8")
        return str(index_path)
