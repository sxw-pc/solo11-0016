"""SQLite 持久化封装：任务、结果、校验、溯源与导出记录。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from archive_extractor.utils.common import ensure_dir, utc_now
from archive_extractor.storage.db_schema import (
    MIGRATION_COLUMNS,
    SCHEMA_INDEXES,
    SCHEMA_TABLES,
    TASK_FIELDS,
)

# 允许进入仲裁撤销日志的表，杜绝表名注入。
JOURNAL_ENTITIES = {
    "results", "traces", "validations", "dedup_reports", "task_files",
}


class SQLiteClient:
    """线程安全、单文件 SQLite 客户端。"""

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        ensure_dir(self.path.parent)
        self._lock = threading.RLock()
        self._in_tx = False
        self._columns_cache: dict[str, list[str]] = {}
        self.conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=15
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            # 先建表，再对旧表补列，最后建索引——索引可能引用新增列。
            self.conn.executescript(SCHEMA_TABLES)
            self._migrate()
            self.conn.executescript(SCHEMA_INDEXES)
            self.conn.commit()

    def _migrate(self) -> None:
        """对旧版本数据库补齐新增列，新表由 CREATE TABLE IF NOT EXISTS 覆盖。"""
        for table, columns in MIGRATION_COLUMNS.items():
            existing = {
                row["name"] for row in self.conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            for name, declaration in columns.items():
                if name not in existing:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                    )

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def begin_immediate(self) -> None:
        """开启写事务并持有锁，直到 commit/rollback；用于仲裁提交与整批撤销。"""
        if self._in_tx:
            raise RuntimeError("当前线程已存在未结束的事务")
        self._lock.acquire()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self._in_tx = True
        except Exception:
            self._lock.release()
            raise

    def commit(self) -> None:
        if not self._in_tx:
            raise RuntimeError("没有待提交的事务")
        try:
            self.conn.commit()
        finally:
            self._in_tx = False
            self._lock.release()

    def rollback(self) -> None:
        if not self._in_tx:
            return
        try:
            self.conn.rollback()
        finally:
            self._in_tx = False
            self._lock.release()

    @property
    def in_transaction(self) -> bool:
        return self._in_tx

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cursor = self.conn.execute(sql, params)
            if not self._in_tx:
                self.conn.commit()
            if sql.lstrip().upper().startswith("INSERT"):
                return int(cursor.lastrowid or 0)
            return int(cursor.rowcount)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        with self._lock:
            self.conn.executemany(sql, rows)
            if not self._in_tx:
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

    # 仲裁批次与撤销日志
    def table_columns(self, table: str) -> list[str]:
        cached = self._columns_cache.get(table)
        if cached is not None:
            return cached
        rows = self.query(f"PRAGMA table_info({table})")
        columns = [row["name"] for row in rows]
        self._columns_cache[table] = columns
        return columns

    def create_arbitration_batch(self, payload: dict) -> int:
        return self.execute(
            """INSERT INTO arbitration_batches
               (batch_no, task_id, ruleset_signature, arbiter_version,
                rule_versions_json, status, candidate_count, group_count,
                merged_group_count, conflict_count, summary_json,
                created_at, committed_at)
               VALUES (?, ?, ?, ?, ?, 'committed', ?, ?, ?, ?, ?, ?, ?)""",
            (payload["batch_no"], payload["task_id"],
             payload.get("ruleset_signature", ""), payload["arbiter_version"],
             json.dumps(payload.get("rule_versions", []), ensure_ascii=False),
             int(payload.get("candidate_count", 0)),
             int(payload.get("group_count", 0)),
             int(payload.get("merged_group_count", 0)),
             int(payload.get("conflict_count", 0)),
             json.dumps(payload.get("summary", {}), ensure_ascii=False),
             utc_now(), payload.get("committed_at", utc_now())),
        )

    def get_arbitration_batch(self, batch_id: int) -> Optional[dict]:
        return self.query_one(
            "SELECT * FROM arbitration_batches WHERE id = ?", (batch_id,)
        )

    def get_arbitration_batch_by_no(self, batch_no: str) -> Optional[dict]:
        return self.query_one(
            "SELECT * FROM arbitration_batches WHERE batch_no = ?", (batch_no,)
        )

    def list_arbitration_batches(
        self, task_id: Optional[int] = None, limit: int = 100
    ) -> list[dict]:
        if task_id is None:
            return self.query(
                """SELECT * FROM arbitration_batches ORDER BY id DESC LIMIT ?""",
                (int(limit),),
            )
        return self.query(
            """SELECT * FROM arbitration_batches WHERE task_id = ?
               ORDER BY id DESC LIMIT ?""",
            (task_id, int(limit)),
        )

    def mark_batch_rolled_back(
        self, batch_id: int, reason: str = "manual_rollback"
    ) -> None:
        self.execute(
            """UPDATE arbitration_batches
               SET status = 'rolled_back', rolled_back_at = ?,
                   rollback_reason = ? WHERE id = ?""",
            (utc_now(), reason, batch_id),
        )

    def create_arbitration_group(self, payload: dict) -> int:
        return self.execute(
            """INSERT INTO arbitration_groups
               (batch_id, rule_id, form_type, logical_key, winner_result_id,
                merged_data_json, original_data_json, candidate_count,
                had_conflict, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (payload["batch_id"], payload.get("rule_id", ""),
             payload.get("form_type", ""), payload.get("logical_key", ""),
             payload.get("winner_result_id"),
             json.dumps(payload.get("merged_data", {}), ensure_ascii=False),
             json.dumps(payload.get("original_data", {}), ensure_ascii=False),
             int(payload.get("candidate_count", 1)),
             1 if payload.get("had_conflict") else 0, utc_now()),
        )

    def set_group_winner(self, group_id: int, winner_result_id: int) -> None:
        self.execute(
            "UPDATE arbitration_groups SET winner_result_id = ? WHERE id = ?",
            (winner_result_id, group_id),
        )

    def list_arbitration_groups(self, batch_id: int) -> list[dict]:
        return self.query(
            "SELECT * FROM arbitration_groups WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        )

    def add_arbitration_field_records(self, rows: list[dict]) -> None:
        self.executemany(
            """INSERT INTO arbitration_field_records
               (batch_id, group_id, result_id, field_path, adopted,
                field_value_json, is_empty, validation_level, source_mtime,
                source_rel_path, source_md5, source_line, selector,
                selector_type, rule_version, rank_score, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    row["batch_id"], row["group_id"], row.get("result_id"),
                    row["field_path"], 1 if row.get("adopted") else 0,
                    json.dumps(row.get("value"), ensure_ascii=False),
                    1 if row.get("is_empty") else 0,
                    row.get("validation_level", ""),
                    float(row.get("source_mtime", 0) or 0),
                    row.get("source_rel_path", ""), row.get("source_md5", ""),
                    row.get("source_line"), row.get("selector", ""),
                    row.get("selector_type", ""), row.get("rule_version", ""),
                    int(row.get("rank_score", 0)), row.get("reason", ""),
                    utc_now(),
                )
                for row in rows
            ],
        )

    def list_arbitration_field_records(self, batch_id: int) -> list[dict]:
        return self.query(
            """SELECT * FROM arbitration_field_records WHERE batch_id = ?
               ORDER BY group_id, id""",
            (batch_id,),
        )

    def field_records_for_result(self, result_id: int) -> list[dict]:
        return self.query(
            """SELECT * FROM arbitration_field_records WHERE result_id = ?
               ORDER BY id""",
            (result_id,),
        )

    def adopted_field_records(self, batch_id: int) -> list[dict]:
        return self.query(
            """SELECT * FROM arbitration_field_records
               WHERE batch_id = ? AND adopted = 1 ORDER BY id""",
            (batch_id,),
        )

    def add_arbitration_conflicts(self, rows: list[dict]) -> None:
        self.executemany(
            """INSERT INTO arbitration_conflicts
               (batch_id, group_id, task_id, rule_id, logical_key, field_path,
                incumbent_result_id, incoming_result_id, incumbent_value_json,
                incoming_value_json, incumbent_source, incoming_source,
                resolution, summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    row["batch_id"], row.get("group_id"), row.get("task_id"),
                    row.get("rule_id", ""), row.get("logical_key", ""),
                    row.get("field_path", ""),
                    row.get("incumbent_result_id"),
                    row.get("incoming_result_id"),
                    json.dumps(row.get("incumbent_value"), ensure_ascii=False),
                    json.dumps(row.get("incoming_value"), ensure_ascii=False),
                    row.get("incumbent_source", ""),
                    row.get("incoming_source", ""),
                    row.get("resolution", "recorded"), row.get("summary", ""),
                    utc_now(),
                )
                for row in rows
            ],
        )

    def list_arbitration_conflicts(self, batch_id: int) -> list[dict]:
        return self.query(
            "SELECT * FROM arbitration_conflicts WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        )

    def journal(
        self,
        batch_id: int,
        entity: str,
        entity_id: Optional[int],
        operation: str,
        before: Optional[dict],
    ) -> None:
        if entity not in JOURNAL_ENTITIES:
            raise ValueError(f"仲裁撤销日志不支持的实体: {entity}")
        self.execute(
            """INSERT INTO arbitration_journal
               (batch_id, entity, entity_id, operation, before_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (batch_id, entity, entity_id, operation,
             json.dumps(before, ensure_ascii=False) if before is not None else None,
             utc_now()),
        )

    def list_journal(self, batch_id: int) -> list[dict]:
        return self.query(
            """SELECT * FROM arbitration_journal WHERE batch_id = ?
               ORDER BY id DESC""",
            (batch_id,),
        )

    def find_active_result(
        self, task_id: int, rule_id: str, logical_key: str
    ) -> Optional[dict]:
        """按规则与逻辑主键查找当前有效结果，供跨批冲突检测。"""
        rows = self.query(
            """SELECT * FROM results WHERE task_id = ? AND rule_id = ?
               AND is_deleted = 0 AND status IN ('valid', 'warning')""",
            (task_id, rule_id),
        )
        return rows[0] if rows else None

    def active_results_for_rule(self, rule_id: str) -> list[dict]:
        """跨任务查找某规则当前有效结果，用于并发批次写同一逻辑表单的检测。"""
        return self.query(
            """SELECT * FROM results WHERE rule_id = ? AND is_deleted = 0
               AND status IN ('valid', 'warning') ORDER BY id""",
            (rule_id,),
        )
