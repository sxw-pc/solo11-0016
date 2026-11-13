"""失败隔离台账与复检记录的持久化方法（由 SQLiteClient 混入）。"""
from __future__ import annotations

from typing import Any, Optional

from archive_extractor.core.failures import build_failure
from archive_extractor.utils.common import utc_now


class FailureStoreMixin:
    """failure_records / recheck_runs 的读写；依赖宿主的 execute/query 方法。"""

    def record_failure(
        self,
        task_id: int,
        rel_path: str,
        failure,
        file_id: Optional[int] = None,
        source_md5: Optional[str] = None,
        source: str = "task",
    ) -> int:
        """按 (task_id, rel_path) 幂等落账：同一文件重复失败只保留一条记录。

        ``source='task'`` 表示正常入库链路的失败，重复命中时累加 occurrences；
        ``source='recheck'`` 表示只读复检，只更新状态，不累加 occurrences。
        """
        now = utc_now()
        row = self.query_one(
            "SELECT * FROM failure_records WHERE task_id = ? AND rel_path = ?",
            (task_id, rel_path),
        )
        payload = (
            failure.category, failure.reason, failure.stage, failure.message[:800],
        )
        if row:
            occurrences = int(row["occurrences"] or 0) + (
                1 if source == "task" else 0
            )
            self.execute(
                """UPDATE failure_records SET category = ?, reason = ?, stage = ?,
                   message = ?, source_md5 = COALESCE(?, source_md5),
                   occurrences = ?, status = 'isolated', source = ?,
                   updated_at = ? WHERE task_id = ? AND rel_path = ?""",
                (*payload, source_md5, occurrences, source, now, task_id, rel_path),
            )
            return int(row["id"])
        return self.execute(
            """INSERT INTO failure_records
               (task_id, file_id, rel_path, source_md5, category, reason, stage,
                message, occurrences, status, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'isolated', ?, ?, ?)""",
            (task_id, file_id, rel_path, source_md5, *payload, source, now, now),
        )

    def failure_rows(self, task_id: int) -> list[dict]:
        return self.query(
            "SELECT * FROM failure_records WHERE task_id = ? ORDER BY id",
            (task_id,),
        )

    def clear_failure(self, task_id: int, rel_path: str) -> None:
        """文件在重新执行中成功时撤销隔离记录，避免台账长期残留。"""
        self.execute(
            "DELETE FROM failure_records WHERE task_id = ? AND rel_path = ?",
            (task_id, rel_path),
        )

    def clear_failures_except(self, task_id: int, rel_paths: set[str]) -> int:
        """清理已不存在的隔离文件（扫描阶段标记 removed 的场景）。"""
        rows = self.failure_rows(task_id)
        stale = [row for row in rows if row["rel_path"] not in rel_paths]
        for row in stale:
            self.execute("DELETE FROM failure_records WHERE id = ?", (row["id"],))
        return len(stale)

    def update_failure_record(self, record_id: int, **fields: Any) -> None:
        allowed = {"status", "recheck_count", "last_recheck_at", "message",
                   "category", "reason", "stage", "updated_at"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates.setdefault("updated_at", utc_now())
        clause = ", ".join(f"{key} = ?" for key in updates)
        self.execute(
            f"UPDATE failure_records SET {clause} WHERE id = ?",
            tuple(updates.values()) + (record_id,),
        )

    def apply_recheck_result(self, record: dict, status: str, reason: str,
                             category: str, stage: str, message: str) -> None:
        """复检结果只更新现有台账行；同一文件不会新增第二条记录。"""
        record_id = record.get("id")
        if not record_id:
            # 旧库补齐出来的失败行还没有台账实体，复检时补建一条，仍按唯一键幂等。
            failure = build_failure(reason, message)
            failure.category, failure.stage = category, stage
            self.record_failure(
                record.get("task_id"), record.get("rel_path"), failure,
                file_id=record.get("file_id"),
                source_md5=record.get("source_md5"), source="recheck",
            )
            row = self.query_one(
                "SELECT id FROM failure_records WHERE task_id = ? AND rel_path = ?",
                (record.get("task_id"), record.get("rel_path")),
            )
            record_id = row["id"] if row else None
        if record_id:
            self.update_failure_record(
                record_id, status=status, reason=reason, category=category,
                stage=stage, message=message,
                recheck_count=int(record.get("recheck_count") or 0) + 1,
                last_recheck_at=utc_now(),
            )

    def add_recheck_run(self, task_id: int, checked: int, recovered: int,
                        still_failed: int) -> int:
        return self.execute(
            """INSERT INTO recheck_runs
               (task_id, checked, recovered, still_failed, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (task_id, checked, recovered, still_failed, utc_now()),
        )

    def recheck_runs(self, task_id: int) -> list[dict]:
        return self.query(
            "SELECT * FROM recheck_runs WHERE task_id = ? ORDER BY id", (task_id,)
        )
