"""任务调度、失败隔离、并发执行与断点续跑。"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Optional

from archive_extractor.config.loader import load_rules, rule_version_payload
from archive_extractor.core.deduplicator import Deduplicator
from archive_extractor.core.encoder import read_html
from archive_extractor.core.linkage import LinkageEngine
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
        """执行任务；limit 用于演示、测试及可控批量处理。"""
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
        linkage_engines = {
            rule["rule_id"]: LinkageEngine(rule, rule["_linkage_graph"])
            for rule in ruleset.all()
        }
        workers = max(1, int(config.get("concurrency", 1)))
        remaining_limit = limit if limit is not None else self.max_files
        processed = 0
        self.deduplicator.begin(task_id)
        try:
            while True:
                current = self._task(task_id)
                if current["status"] in {"paused", "cancelled"}:
                    break
                pending = self.db.task_files(task_id, ["pending"])
                if not pending:
                    break
                if remaining_limit is not None:
                    pending = pending[: max(0, remaining_limit - processed)]
                    if not pending:
                        self.db.update_task(task_id, status="paused")
                        break
                batch = pending[:workers]
                outcomes = self._run_batch(
                    batch, extractor, linkage_engines, ruleset, task_id
                )
                processed += len(batch)
                self.db.refresh_task_counts(task_id)
                if any(outcome.get("cancelled") for outcome in outcomes):
                    break
            self._finish(task_id)
        except Exception as exc:
            self.db.update_task(
                task_id, status="failed", error_message=str(exc), finished_at=utc_now()
            )
            LOGGER.exception("任务 %s 执行失败", task_id)
            raise
        finally:
            self.deduplicator.clear_cache(task_id)
        return self._task(task_id)

    def _run_batch(self, batch, extractor, linkage_engines, ruleset,
                   task_id) -> list[dict]:
        workers = min(len(batch), max(1, int(self._concurrency(task_id))))
        if workers == 1:
            return [
                self._process_file(
                    batch[0], extractor, linkage_engines, ruleset, task_id
                )
            ]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    self._process_file, row, extractor, linkage_engines,
                    ruleset, task_id,
                )
                for row in batch
            ]
            return [future.result() for future in futures]

    def _process_file(self, row, extractor, linkage_engines, ruleset,
                      task_id) -> dict:
        self.db.update_task_file(row["id"], status="processing")
        try:
            extracted = self._extract_with_timeout(row, extractor, ruleset)
            rule = ruleset.get(extracted["rule_id"])
            linkage = linkage_engines[extracted["rule_id"]]
            linkage.evaluate(extracted)
            linkage_block = extracted.get("linkage", {})
            issues = validate_data(rule, extracted["data"])
            for error in extracted.get("errors", []):
                issues.append(
                    ValidationIssue(
                        "error", error["field"], error["message"], "conversion"
                    )
                )
            for issue in linkage_block.get("issues", []):
                issues.append(
                    ValidationIssue(
                        issue["level"], issue["field"], issue["message"],
                        issue["validator"],
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
                all_traces = list(extracted["traces"]) + linkage_block.get(
                    "traces", []
                )
                for trace in linkage_block.get("traces", []):
                    trace.setdefault("source_md5", row["md5"])
                self.tracer.record(
                    result_id, all_traces, row["md5"],
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
            return {"file": row["rel_path"], "status": "success"}
        except (FutureTimeout, TimeoutError):
            return self._fail(row, f"处理超时（{self.timeout:g} 秒）")
        except Exception as exc:
            LOGGER.warning("文件处理失败 %s: %s", row["rel_path"], exc)
            return self._fail(row, str(exc))

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
        pending = self.db.task_files(task_id, ["pending", "processing"])
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

    def rederive(self, task_id: int, rel_path: str,
                 changed_fields: list[str]) -> dict:
        """不重新解析源文件，仅按依赖图重算受影响的派生字段与约束。"""
        from archive_extractor.core.rederive import rederive_result

        return rederive_result(self, task_id, rel_path, changed_fields)

    def delete(self, task_id: int) -> None:
        if self._task(task_id)["status"] == "running":
            raise RuntimeError("运行中的任务不能删除")
        self.db.delete_task(task_id)
