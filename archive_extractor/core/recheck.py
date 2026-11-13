"""隔离文件的只读复检。

复检只做“读取 → 解析 → 归类”，不写 results、不建快照、不触发去重、不改
task_files 状态，因此不会产生重复入库结果。台账仅更新同一 (task_id, rel_path)
行的复检状态与次数，记录条数不会随复检次数增长。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Optional

from archive_extractor.core.encoder import read_html
from archive_extractor.core.failures import (
    build_failure,
    classify_exception,
    collect_isolated,
    reason_meta,
)
from archive_extractor.core.parser import FormExtractor
from archive_extractor.utils.logger import get_logger

LOGGER = get_logger("archive_extractor.recheck")


class Rechecker:
    """对隔离文件执行幂等的只读复检。"""

    def __init__(self, db, ruleset, timeout: float = 10) -> None:
        self.db = db
        self.ruleset = ruleset
        self.timeout = float(timeout)
        self.extractor = FormExtractor(ruleset)

    def run(self, task_id: int, limit: Optional[int] = None) -> dict:
        """复检任务的隔离文件，返回汇总；不修改入库相关数据。"""
        task = self.db.get_task(task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")
        records = collect_isolated(self.db, task_id)
        if limit is not None:
            records = records[: max(0, int(limit))]
        checked = recovered = still_failed = 0
        details = []
        for record in records:
            outcome = self._check_one(task, record)
            checked += 1
            if outcome["status"] == "recovered":
                recovered += 1
            else:
                still_failed += 1
            details.append(outcome)
        if records:
            self.db.add_recheck_run(task_id, checked, recovered, still_failed)
        return {
            "task_id": task_id,
            "checked": checked,
            "recovered": recovered,
            "still_failed": still_failed,
            "records": len(collect_isolated(self.db, task_id)),
            "details": details,
        }

    def _check_one(self, task: dict, record: dict) -> dict:
        """单个隔离文件复检；任何阶段异常都只归类，不抛出。"""
        rel_path = record["rel_path"]
        abs_path = self._abs_path(task, record)
        previous = record.get("reason")
        try:
            failure = self._parse(abs_path)
        except Exception as exc:  # 复检必须不中断整批
            failure = classify_exception(exc)
        if failure is None:
            status = "recovered"
            message = "只读复检已可解析，未重新入库"
            # 保留原失败原因，复检结果由 status 表达，避免分类随复检漂移。
            reason = previous or "recovered"
            category = record.get("category") or reason_meta(reason)[0]
            stage = record.get("stage") or reason_meta(reason)[1]
        else:
            status = "isolated"
            reason, category, stage = failure.reason, failure.category, failure.stage
            message = failure.message
        self.db.apply_recheck_result(
            record, status, reason, category, stage, message
        )
        if previous and previous != reason:
            LOGGER.info("复检原因变化 %s: %s -> %s", rel_path, previous, reason)
        return {
            "rel_path": rel_path,
            "status": status,
            "reason": reason,
            "category": category,
            "stage": stage,
            "message": message,
            "reason_changed": bool(previous and previous != reason),
        }

    def _parse(self, abs_path: Path):
        """返回 None 表示可解析；否则返回失败分类。"""

        def work():
            text, _ = read_html(abs_path)
            return self.extractor.extract(text, str(abs_path))

        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(work)
        try:
            future.result(timeout=self.timeout)
            return None
        except (FutureTimeout, TimeoutError) as exc:
            return build_failure("timeout", str(exc) or "复检超时")
        except Exception as exc:
            return classify_exception(exc)
        finally:
            pool.shutdown(wait=False)

    @staticmethod
    def _abs_path(task: dict, record: dict) -> Path:
        """优先用台账文件行里的绝对路径（扫描后文件可能被移动），否则拼根目录。"""
        if record.get("abs_path"):
            return Path(record["abs_path"])
        rel_path = record["rel_path"]
        return Path(task["root_dir"]) / rel_path
