"""任务调度、失败隔离、并发执行与断点续跑。

链路顺序（仲裁是解析/校验之后、落库之前的独立可回放阶段）：

    扫描 → 解析 → 校验 → 【字段级仲裁 Arbiter】 → 落库（含快照/溯源）
         → 既有去重（语义不变） → 导出

同一批任务里同一逻辑表单的多份候选先在内存中做字段级合并，再统一在一个
写事务里落库；全部写动作进入仲裁撤销日志，支持按批次号整体撤销。
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Optional

from archive_extractor.config.loader import load_rules, rule_version_payload
from archive_extractor.core.arbiter import Arbiter
from archive_extractor.core.deduplicator import Deduplicator
from archive_extractor.core.encoder import read_html
from archive_extractor.core.parser import FormExtractor
from archive_extractor.core.scanner import DirectoryScanner
from archive_extractor.core.tracer import Tracer
from archive_extractor.core.validator import (
    ValidationIssue,
    highest_level,
    issues_to_dicts,
    validate_data,
)
from archive_extractor.storage.file_store import FileStore
from archive_extractor.utils.common import utc_now
from archive_extractor.utils.logger import get_logger

LOGGER = get_logger("archive_extractor.task")


class TaskRunner:
    """执行采集任务；所有异常在单文件粒度和任务粒度双重隔离。"""

    def __init__(
        self,
        db,
        data_dir: Path | str,
        timeout: float = 10,
        max_files: Optional[int] = None,
    ) -> None:
        self.db = db
        self.store = FileStore(data_dir)
        self.tracer = Tracer(db, self.store)
        self.deduplicator = Deduplicator(db)
        self.arbiter = Arbiter(db, self.store)
        self.timeout = float(timeout)
        self.max_files = max_files
        self._persist_lock = threading.RLock()

    def create_task(
        self,
        name: str,
        root_dir: str,
        rules_dir: Optional[str] = None,
        config: Optional[dict] = None,
    ) -> int:
        """创建任务；目录与规则在首次执行时再加载。"""
        root = str(Path(root_dir).expanduser().resolve())
        rules = str(Path(rules_dir).expanduser().resolve()) if rules_dir else ""
        payload = {
            "timeout": self.timeout,
            "concurrency": 1,
            "scan": {},
        }
        payload.update(config or {})
        task_id = self.db.create_task(name, root, rules, payload)
        self.prepare(task_id)
        return task_id

    def prepare(self, task_id: int) -> dict:
        """扫描目录并增量更新待处理清单。"""
        task = self._task(task_id)
        config = json.loads(task.get("config_json") or "{}")
        scan_cfg = config.get("scan", {})
        scanner = DirectoryScanner(
            task["root_dir"],
            exclude_dirs=scan_cfg.get("exclude_dirs"),
            exclude_suffixes=scan_cfg.get("exclude_suffixes"),
            min_size=scan_cfg.get("min_size", 0),
            max_size=scan_cfg.get("max_size"),
        )
        items = scanner.scan()
        current_paths = {item.rel_path for item in items}
        self.db.mark_removed(task_id, current_paths)
        new, changed, new_items = 0, 0, []
        for item in items:
            existing = self.db.get_task_file(task_id, item.rel_path)
            if not existing:
                new_items.append(item)
                new += 1
                continue
            fields = {
                "abs_path": item.abs_path,
                "size": item.size,
                "mtime": item.mtime,
                "md5": item.md5,
            }
            if existing["md5"] != item.md5:
                fields.update(status="pending", error=None, result_id=None)
                self.db.update_task_file(existing["id"], **fields)
                changed += 1
            elif existing["status"] in {"removed", "processing"}:
                fields.update(status="pending", error=None)
                self.db.update_task_file(existing["id"], **fields)
            else:
                self.db.update_task_file(existing["id"], **fields)
        if new_items:
            now = utc_now()
            self.db.executemany(
                """INSERT INTO task_files
                   (task_id, rel_path, abs_path, size, mtime, md5, status,
                    updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                [(task_id, item.rel_path, item.abs_path, item.size, item.mtime,
                  item.md5, now) for item in new_items],
            )
        counts = self.db.refresh_task_counts(task_id)
        return {**counts, "new": new, "changed": changed, "removed": len(
            [row for row in self.db.task_files(task_id) if row["status"] == "removed"]
        )}

    def run(self, task_id: int, limit: Optional[int] = None) -> dict:
        """执行任务；limit 用于演示、测试及可控批量处理。

        一次 run 调用对应一个仲裁批次：先并行完成解析与校验（阶段一，不落
        业务库），结束后统一进入字段级仲裁并在单事务内落库（阶段二）。
        """
        task = self._task(task_id)
        if task["status"] == "cancelled":
            raise RuntimeError("任务已取消，不能继续执行")
        self.db.update_task(
            task_id, status="running", started_at=task.get("started_at") or utc_now(),
            finished_at=None, error_message=None,
        )
        self.db.reset_processing(task_id)
        self.prepare(task_id)
        config = json.loads(task.get("config_json") or "{}")
        try:
            ruleset = load_rules(task["rules_dir"] or None)
        except Exception as exc:
            self.db.update_task(task_id, status="failed", error_message=str(exc),
                                finished_at=utc_now())
            raise
        self.db.save_rule_versions(rule_version_payload(ruleset))
        extractor = FormExtractor(ruleset)
        workers = max(1, int(config.get("concurrency", 1)))
        remaining_limit = limit if limit is not None else self.max_files
        staged: list[dict] = []
        try:
            while True:
                current = self._task(task_id)
                if current["status"] in {"paused", "cancelled"}:
                    break
                pending = self.db.task_files(task_id, ["pending"])
                if not pending:
                    break
                if remaining_limit is not None:
                    pending = pending[: max(0, remaining_limit - len(staged))]
                    if not pending:
                        self.db.update_task(task_id, status="paused")
                        break
                batch = pending[:workers]
                outcomes = self._run_batch(
                    batch, extractor, ruleset, task_id, staged
                )
                self.db.refresh_task_counts(task_id)
                if any(outcome.get("cancelled") for outcome in outcomes):
                    break
            # 阶段二：仲裁 + 单事务落库（独立、可回放、可整批撤销）。
            if staged:
                self._commit_stage(task_id, staged, ruleset)
            self.deduplicator.clear_cache(task_id)
            self.db.refresh_task_counts(task_id)
            self._finish(task_id)
        except Exception as exc:
            self.db.update_task(
                task_id, status="failed", error_message=str(exc), finished_at=utc_now()
            )
            LOGGER.exception("任务 %s 执行失败", task_id)
            raise
        return self._task(task_id)

    def _run_batch(
        self, batch, extractor, ruleset, task_id, staged: list[dict]
    ) -> list[dict]:
        workers = min(len(batch), max(1, int(self._concurrency(task_id))))
        if workers == 1:
            return [
                self._stage_file(batch[0], extractor, ruleset, task_id, staged)
            ]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    self._stage_file, row, extractor, ruleset, task_id, staged
                )
                for row in batch
            ]
            return [future.result() for future in futures]

    def _stage_file(
        self, row, extractor, ruleset, task_id, staged: list[dict]
    ) -> dict:
        """阶段一：解析 + 校验，只暂存内存候选，不写业务结果。"""
        self.db.update_task_file(row["id"], status="processing")
        try:
            extracted = self._extract_with_timeout(row, extractor, ruleset)
            rule = ruleset.get(extracted["rule_id"])
            issues = validate_data(rule, extracted["data"])
            for error in extracted.get("errors", []):
                issues.append(
                    ValidationIssue(
                        "error", error["field"], error["message"], "conversion"
                    )
                )
            level = highest_level(issues)
            status = "invalid" if level == "error" else (
                "warning" if level == "warning" else "valid"
            )
            candidate = {
                "task_id": task_id,
                "file_row": row,
                "form_type": extracted["form_type"],
                "rule_id": extracted["rule_id"],
                "rule_version": extracted["rule_version"],
                "rule_content_hash": extracted["rule_content_hash"],
                "data": extracted["data"],
                "traces": extracted["traces"],
                "issues": issues_to_dicts(issues),
                "status": status,
                "error_level": level,
                "rel_path": row["rel_path"],
                "md5": row["md5"],
                "mtime": row["mtime"],
                "dedup_config": rule.get("dedup", {}),
            }
            staged.append(candidate)
            return {"file": row["rel_path"], "status": "staged"}
        except (FutureTimeout, TimeoutError):
            return self._fail(row, f"处理超时（{self.timeout:g} 秒）")
        except Exception as exc:
            LOGGER.warning("文件处理失败 %s: %s", row["rel_path"], exc)
            return self._fail(row, str(exc))

    def _commit_stage(self, task_id: int, staged: list[dict], ruleset) -> dict:
        """阶段二：把暂存候选交给仲裁器，在单事务内落库并记录撤销日志。"""

        def persist_callback(batch_id: int, group: dict, champion: dict) -> int:
            champion_id = self._persist_candidate(
                batch_id, task_id, group, champion, merged=True
            )
            # 组内其余成员仍各自落一条原始结果（保持“每文件一结果”的既有
            # 行数与溯源语义），随后交给既有去重逻辑做逻辑删除；其被仲裁
            # 淘汰的字段值已完整进入仲裁档案。
            for member in group["members"]:
                if member["order"] == champion["order"]:
                    continue
                self._persist_candidate(
                    batch_id, task_id, group, member, merged=False,
                    winner_id=champion_id,
                )
            return champion_id

        summary = self.arbiter.commit_batch(
            task_id, staged, ruleset, persist_callback, self.deduplicator
        )
        LOGGER.info(
            "任务 %s 仲裁批次 %s 完成：候选 %s，合并组 %s，跨批冲突 %s",
            task_id, summary["batch_no"], summary["candidate_count"],
            summary["merged_group_count"], summary["conflict_count"],
        )
        return summary

    def _persist_candidate(
        self,
        batch_id: int,
        task_id: int,
        group: dict,
        candidate: dict,
        merged: bool,
        winner_id: Optional[int] = None,
    ) -> int:
        """在仲裁事务内落库一个候选；每个写动作都进撤销日志。

        merged=True 落字段级合并后的冠军数据；merged=False 落成员原始数据，
        保持每文件一条结果，供既有去重逻辑删除与历史溯源。
        """
        row = candidate["file_row"]
        snapshot = self.tracer.make_snapshot(
            row["abs_path"], row["md5"], Path(row["rel_path"]).name
        )
        version = self.db.next_result_version(task_id, row["rel_path"])
        if version > 1:
            superseded = self.db.query(
                """SELECT * FROM results WHERE task_id = ?
                   AND source_rel_path = ? AND is_deleted = 0""",
                (task_id, row["rel_path"]),
            )
            for old in superseded:
                # update 镜像用于撤销时整行恢复；supersede 标记用于在去重
                # 之后重新确认新版本取代语义不被 duplicate 覆盖。
                self.db.journal(
                    batch_id, "results", old["id"], "update", old
                )
                self.db.journal(
                    batch_id, "results", old["id"], "supersede", None
                )
                self.db.execute(
                    """UPDATE results SET status = 'superseded',
                       is_deleted = 1, deleted_reason = 'new_version',
                       updated_at = ? WHERE id = ?""",
                    (utc_now(), old["id"]),
                )
        result_id = self.db.store_result({
            "task_id": task_id, "file_id": row["id"],
            "form_type": candidate["form_type"],
            "rule_id": candidate["rule_id"],
            "rule_version": candidate["rule_version"],
            "rule_content_hash": candidate["rule_content_hash"],
            # 冠军落字段级仲裁后的合并数据；其余成员保留各自原始提取值。
            "data": group["merged_data"] if merged else candidate["data"],
            "status": candidate["status"],
            "error_level": candidate["error_level"],
            "source_rel_path": row["rel_path"],
            "source_md5": row["md5"], "source_mtime": row["mtime"],
            "snapshot_path": snapshot, "version": version,
        })
        self.db.execute(
            "UPDATE results SET arbitration_batch_id = ? WHERE id = ?",
            (batch_id, result_id),
        )
        self.db.journal(batch_id, "results", result_id, "insert", None)
        for issue in candidate["issues"]:
            issue_id = self.db.execute(
                """INSERT INTO validations
                   (result_id, level, field, message, validator)
                   VALUES (?, ?, ?, ?, ?)""",
                (result_id, issue["level"], issue["field"], issue["message"],
                 issue.get("validator", "")),
            )
            self.db.journal(batch_id, "validations", issue_id, "insert", None)
        # 溯源落在各结果上：行号/选择器/规则版本可直接定位字段值来源。
        for trace in candidate["traces"]:
            trace_id = self.db.execute(
                """INSERT INTO traces
                   (result_id, field, selector, selector_type, source_line,
                    raw_value, extracted_at, rule_version, source_md5)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (result_id, trace["field"], trace["selector"],
                 trace["selector_type"], trace["source_line"],
                 trace["raw_value"], trace.get("extracted_at", utc_now()),
                 candidate["rule_version"], row["md5"]),
            )
            self.db.journal(batch_id, "traces", trace_id, "insert", None)
        file_before = self.db.get_task_file(task_id, row["rel_path"])
        # 成员文件的 result_id 指向冠军结果，保证 task_files 始终指向
        # 承载合并字段值的有效结果。
        self.db.update_task_file(
            row["id"], status="success", error=None,
            result_id=winner_id if winner_id is not None else result_id,
        )
        self.db.journal(
            batch_id, "task_files", row["id"], "update", file_before
        )
        candidate["result_id"] = result_id
        return result_id

    def _extract_with_timeout(self, row, extractor, ruleset) -> dict:
        def work():
            text, _ = read_html(row["abs_path"])
            return extractor.extract(text, row["rel_path"])

        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(work)
        try:
            return future.result(timeout=self.timeout)
        finally:
            pool.shutdown(wait=False)

    def _fail(self, row, message: str) -> dict:
        self.db.update_task_file(row["id"], status="failed", error=message[:800])
        return {"file": row["rel_path"], "status": "failed", "error": message}

    def _finish(self, task_id: int) -> None:
        task = self._task(task_id)
        if task["status"] in {"paused", "cancelled"}:
            return
        # 阶段一暂存的文件处于 processing，提交阶段已统一置为 success；
        # 因此完成判定只看是否还有未开始的 pending。
        pending = self.db.task_files(task_id, ["pending"])
        if pending:
            self.db.update_task(task_id, status="paused")
        else:
            self.db.update_task(task_id, status="completed", finished_at=utc_now())

    def _concurrency(self, task_id: int) -> int:
        config = json.loads(self._task(task_id).get("config_json") or "{}")
        return int(config.get("concurrency", 1))

    def _task(self, task_id: int) -> dict:
        task = self.db.get_task(task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")
        return task

    def pause(self, task_id: int) -> None:
        self.db.update_task(task_id, status="paused")

    def cancel(self, task_id: int) -> None:
        self.db.update_task(task_id, status="cancelled", finished_at=utc_now())

    def retry(self, task_id: int) -> int:
        """将失败文件重置为待处理，返回重试数量。"""
        count = self.db.retry_failed(task_id)
        self.db.update_task(task_id, status="paused", error_message=None)
        return count

    def delete(self, task_id: int) -> None:
        if self._task(task_id)["status"] == "running":
            raise RuntimeError("运行中的任务不能删除")
        self.db.delete_task(task_id)

    # ------------------------------------------------------------------
    # 仲裁批次的撤销与查询入口
    # ------------------------------------------------------------------
    def rollback_arbitration(
        self, batch_ref: str | int, reason: str = "manual_rollback"
    ) -> dict:
        """按批次号/批次 ID 整体撤销仲裁，恢复批次前字段与逻辑删除状态。"""
        return self.arbiter.rollback_batch(batch_ref, reason)

    def arbitration_batches(self, task_id: Optional[int] = None) -> list[dict]:
        return self.arbiter.db.list_arbitration_batches(task_id)

    def arbitration_detail(self, batch_ref: str | int) -> dict:
        return self.arbiter.batch_detail(batch_ref)
