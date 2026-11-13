"""单文件处理与失败落账（由 TaskRunner 混入）。

处理成功走原入库链路；失败只更新 task_files 兼容字段并写入隔离台账。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path

from archive_extractor.core.encoder import read_html
from archive_extractor.core.failures import classify_exception
from archive_extractor.core.validator import (
    ValidationIssue,
    highest_level,
    issues_to_dicts,
    validate_data,
)
from archive_extractor.utils.common import utc_now
from archive_extractor.utils.logger import get_logger

LOGGER = get_logger("archive_extractor.task")


class FileProcessorMixin:
    """在 TaskRunner 上处理单个任务文件。"""

    def _process_file(self, row, extractor, ruleset, task_id) -> dict:
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
            with self._persist_lock:
                snapshot = self.tracer.make_snapshot(
                    row["abs_path"], row["md5"], Path(row["rel_path"]).name
                )
                version = self.db.next_result_version(task_id, row["rel_path"])
                if version > 1:
                    self.db.execute(
                        """UPDATE results SET status = 'superseded',
                           is_deleted = 1, deleted_reason = 'new_version',
                           updated_at = ? WHERE task_id = ?
                           AND source_rel_path = ? AND is_deleted = 0""",
                        (utc_now(), task_id, row["rel_path"]),
                    )
                    self.deduplicator.begin(task_id)
                result_id = self.db.store_result({
                    "task_id": task_id, "file_id": row["id"],
                    "form_type": extracted["form_type"],
                    "rule_id": extracted["rule_id"],
                    "rule_version": extracted["rule_version"],
                    "rule_content_hash": extracted["rule_content_hash"],
                    "data": extracted["data"], "status": status,
                    "error_level": level, "source_rel_path": row["rel_path"],
                    "source_md5": row["md5"], "source_mtime": row["mtime"],
                    "snapshot_path": snapshot, "version": version,
                })
                self.db.add_validations(result_id, issues_to_dicts(issues))
                self.tracer.record(
                    result_id, extracted["traces"], row["md5"],
                    extracted["rule_version"],
                )
                if status in {"valid", "warning"}:
                    self.deduplicator.apply(
                        task_id, result_id, extracted["data"], row["mtime"],
                        rule.get("dedup", {}),
                    )
            self.db.update_task_file(
                row["id"], status="success", error=None, result_id=result_id
            )
            self.db.clear_failure(task_id, row["rel_path"])
            return {"file": row["rel_path"], "status": "success"}
        except (FutureTimeout, TimeoutError) as exc:
            return self._fail(row, exc, task_id)
        except Exception as exc:
            LOGGER.warning("文件处理失败 %s: %s", row["rel_path"], exc)
            return self._fail(row, exc, task_id)

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

    def _fail(self, row, exc: BaseException, task_id: int) -> dict:
        """把单文件失败落账到隔离台账，同时保留 task_files 的兼容字段。"""
        failure = classify_exception(exc)
        message = str(exc)
        if failure.reason == "timeout":
            message = f"处理超时（{self.timeout:g} 秒）"
        message = message or failure.reason
        self.db.update_task_file(row["id"], status="failed", error=message[:800])
        self.db.record_failure(
            task_id, row["rel_path"], failure, file_id=row["id"],
            source_md5=row.get("md5"), source="task",
        )
        return {
            "file": row["rel_path"], "status": "failed", "error": message,
            "failure": {
                "category": failure.category, "reason": failure.reason,
                "stage": failure.stage,
            },
        }
