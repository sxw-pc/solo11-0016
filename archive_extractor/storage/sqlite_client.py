"""SQLite 持久化封装：任务、结果、校验、溯源与导出记录。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from archive_extractor.utils.common import ensure_dir, utc_now
from archive_extractor.storage.db_schema import SCHEMA, TASK_FIELDS
from archive_extractor.storage.failure_store import FailureStoreMixin


class SQLiteClient(FailureStoreMixin):
    """线程安全、单文件 SQLite 客户端。"""

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        ensure_dir(self.path.parent)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=15
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cursor = self.conn.execute(sql, params)
            self.conn.commit()
            if sql.lstrip().upper().startswith("INSERT"):
                return int(cursor.lastrowid or 0)
            return int(cursor.rowcount)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        with self._lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def query_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # 任务相关
    def create_task(
        self, name: str, root_dir: str, rules_dir: str, config: dict
    ) -> int:
        now = utc_now()
        return self.execute(
            """INSERT INTO tasks
               (name, root_dir, rules_dir, status, config_json, created_at,
                updated_at) VALUES (?, ?, ?, 'pending', ?, ?, ?)""",
            (name, root_dir, rules_dir, json.dumps(config, ensure_ascii=False),
             now, now),
        )

    def update_task(self, task_id: int, **fields: Any) -> None:
        updates = {key: value for key, value in fields.items() if key in TASK_FIELDS}
        if not updates:
            return
        updates["updated_at"] = utc_now()
        clause = ", ".join(f"{key} = ?" for key in updates)
        self.execute(
            f"UPDATE tasks SET {clause} WHERE id = ?",
            tuple(updates.values()) + (task_id,),
        )

    def get_task(self, task_id: int) -> Optional[dict]:
        return self.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def list_tasks(self, limit: int = 100) -> list[dict]:
        return self.query(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (int(limit),)
        )

    def delete_task(self, task_id: int) -> None:
        self.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    # 任务文件相关
    def add_task_file(self, task_id: int, item) -> int:
        now = utc_now()
        return self.execute(
            """INSERT INTO task_files
               (task_id, rel_path, abs_path, size, mtime, md5, status, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (task_id, item.rel_path, item.abs_path, item.size, item.mtime,
             item.md5, now),
        )

    def get_task_file(self, task_id: int, rel_path: str) -> Optional[dict]:
        return self.query_one(
            "SELECT * FROM task_files WHERE task_id = ? AND rel_path = ?",
            (task_id, rel_path),
        )

    def update_task_file(self, file_id: int, **fields: Any) -> None:
        allowed = {"abs_path", "size", "mtime", "md5", "status", "error",
                   "result_id", "updated_at"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates.setdefault("updated_at", utc_now())
        clause = ", ".join(f"{key} = ?" for key in updates)
        self.execute(
            f"UPDATE task_files SET {clause} WHERE id = ?",
            tuple(updates.values()) + (file_id,),
        )

    def task_files(
        self, task_id: int, statuses: Optional[list[str]] = None
    ) -> list[dict]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            return self.query(
                f"""SELECT * FROM task_files WHERE task_id = ? AND
                    status IN ({placeholders}) ORDER BY id""",
                (task_id, *statuses),
            )
        return self.query(
            "SELECT * FROM task_files WHERE task_id = ? ORDER BY id", (task_id,)
        )

    def mark_removed(self, task_id: int, current_paths: set[str]) -> int:
        rows = self.task_files(task_id)
        removed = 0
        for row in rows:
            if row["rel_path"] not in current_paths and row["status"] != "removed":
                self.update_task_file(row["id"], status="removed", error=None)
                removed += 1
        return removed

    def refresh_task_counts(self, task_id: int) -> dict:
        rows = self.query(
            """SELECT status, COUNT(*) AS amount FROM task_files
               WHERE task_id = ? GROUP BY status""",
            (task_id,),
        )
        counts = {row["status"]: row["amount"] for row in rows}
        success = counts.get("success", 0) + counts.get("skipped", 0)
        failed = counts.get("failed", 0)
        total = sum(value for key, value in counts.items() if key != "removed")
        processed = success + failed
        self.update_task(
            task_id,
            total_files=total,
            processed_files=processed,
            success_count=success,
            failed_count=failed,
        )
        return {"total": total, "processed": processed, "success": success,
                "failed": failed, **counts}

    def retry_failed(self, task_id: int) -> int:
        return self.execute(
            """UPDATE task_files SET status = 'pending', error = NULL,
               updated_at = ? WHERE task_id = ? AND status = 'failed'""",
            (utc_now(), task_id),
        )

    def reset_processing(self, task_id: int) -> None:
        self.execute(
            """UPDATE task_files SET status = 'pending' WHERE task_id = ?
               AND status = 'processing'""",
            (task_id,),
        )

    # 规则与结果
    def save_rule_versions(self, payload: list[dict]) -> None:
        rows = [
            (item["rule_id"], item["version"], item["content_hash"],
             item.get("source_path", ""), item.get("loaded_at", utc_now()))
            for item in payload
        ]
        self.executemany(
            """INSERT OR IGNORE INTO rules_versions
               (rule_id, version, content_hash, source_path, loaded_at)
               VALUES (?, ?, ?, ?, ?)""",
            rows,
        )

    def next_result_version(self, task_id: int, rel_path: str) -> int:
        row = self.query_one(
            """SELECT MAX(version) AS version FROM results
               WHERE task_id = ? AND source_rel_path = ?""",
            (task_id, rel_path),
        )
        return int((row or {}).get("version") or 0) + 1

    def store_result(self, payload: dict) -> int:
        now = utc_now()
        return self.execute(
            """INSERT INTO results
               (task_id, file_id, form_type, rule_id, rule_version,
                rule_content_hash, data_json, status, error_level,
                source_rel_path, source_md5, source_mtime, snapshot_path,
                version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (payload["task_id"], payload.get("file_id"), payload.get("form_type"),
             payload.get("rule_id"), payload.get("rule_version"),
             payload.get("rule_content_hash"),
             json.dumps(payload.get("data", {}), ensure_ascii=False),
             payload.get("status", "valid"), payload.get("error_level", ""),
             payload.get("source_rel_path"), payload.get("source_md5"),
             payload.get("source_mtime", 0), payload.get("snapshot_path"),
             payload.get("version", 1), now, now),
        )

    def get_result(self, result_id: int) -> Optional[dict]:
        return self.query_one("SELECT * FROM results WHERE id = ?", (result_id,))

    def result_detail(self, result_id: int) -> Optional[dict]:
        return self.query_one(
            """SELECT r.*, t.name AS task_name, t.rules_dir AS task_rules_dir
               FROM results r LEFT JOIN tasks t ON t.id = r.task_id
               WHERE r.id = ?""",
            (result_id,),
        )

    def list_results(
        self,
        filters: Optional[dict] = None,
        page: int = 1,
        page_size: int = 20,
        include_deleted: bool = False,
    ) -> list[dict]:
        sql, params = self._result_sql(filters, include_deleted, count=False)
        offset = max(0, (int(page) - 1) * int(page_size))
        sql += " ORDER BY r.id DESC LIMIT ? OFFSET ?"
        return self.query(sql, tuple(params) + (int(page_size), offset))

    def count_results(
        self, filters: Optional[dict] = None, include_deleted: bool = False
    ) -> int:
        sql, params = self._result_sql(filters, include_deleted, count=True)
        row = self.query_one(sql, tuple(params))
        return int(row["amount"] if row else 0)

    @staticmethod
    def _result_sql(filters, include_deleted, count):
        fields = "COUNT(*) AS amount" if count else "r.*"
        clauses, params = [], []
        if not include_deleted:
            clauses.append("r.is_deleted = 0")
        filters = filters or {}
        for column, key in (("r.task_id", "task_id"), ("r.form_type", "form_type"),
                            ("r.error_level", "error_level")):
            if filters.get(key) not in (None, ""):
                clauses.append(f"{column} = ?")
                params.append(filters[key])
        if filters.get("q"):
            clauses.append("(r.data_json LIKE ? OR r.source_rel_path LIKE ?)")
            keyword = f"%{filters['q']}%"
            params.extend([keyword, keyword])
        if filters.get("date_from"):
            clauses.append("r.created_at >= ?")
            params.append(filters["date_from"])
        if filters.get("date_to"):
            clauses.append("r.created_at <= ?")
            params.append(filters["date_to"])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return f"SELECT {fields} FROM results r{where}", params

    def add_validations(self, result_id: int, issues: list[dict]) -> None:
        self.executemany(
            """INSERT INTO validations (result_id, level, field, message, validator)
               VALUES (?, ?, ?, ?, ?)""",
            [(result_id, item["level"], item["field"], item["message"],
              item.get("validator", "")) for item in issues],
        )

    def add_dedup_report(self, report: dict) -> int:
        return self.execute(
            """INSERT INTO dedup_reports
               (task_id, result_id, duplicate_of, reason, similarity, action,
                created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (report["task_id"], report["result_id"], report["duplicate_of"],
             report["reason"], report["similarity"], report["action"],
             report["created_at"]),
        )

    def add_export(self, task_id: int, fmt: str, path: str, sha256: str) -> int:
        return self.execute(
            """INSERT INTO exports (task_id, format, path, sha256, created_at)
               VALUES (?, ?, ?, ?, ?)""", (task_id, fmt, path, sha256, utc_now())
        )
