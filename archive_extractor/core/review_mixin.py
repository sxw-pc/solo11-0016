"""隔离台账查看与只读复检（由 TaskRunner 混入）。

这些方法只读取任务与规则、写 failure_records / recheck_runs，不触碰 results、
validations、traces、dedup_reports 或 task_files 状态。
"""
from __future__ import annotations

from typing import Optional

from archive_extractor.config.loader import load_rules
from archive_extractor.core.failures import collect_isolated, summarize
from archive_extractor.core.recheck import Rechecker


class ReviewMixin:
    """任务级隔离台账与复检入口。"""

    def failures(self, task_id: int) -> dict:
        """返回任务的隔离台账与分类汇总，供 CLI 与服务层复检前预览。"""
        records = collect_isolated(self.db, task_id)
        total_files = (self._task(task_id).get("total_files") or 0)
        return {
            "task_id": task_id,
            "summary": summarize(records, total_files),
            "records": records,
            "rechecks": self.db.recheck_runs(task_id),
        }

    def recheck(self, task_id: int, limit: Optional[int] = None) -> dict:
        """只读复检隔离文件：不写结果、不建快照、不去重、不改任务统计。"""
        task = self._task(task_id)
        try:
            ruleset = load_rules(task["rules_dir"] or None)
        except Exception as exc:
            raise RuntimeError(f"规则加载失败，无法复检: {exc}") from exc
        rechecker = Rechecker(self.db, ruleset, timeout=self.timeout)
        return rechecker.run(task_id, limit=limit)
