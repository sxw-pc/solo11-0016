"""旧版本数据库的增量迁移测试（新增仲裁表与 results 批次列）。"""
import sqlite3


def test_legacy_db_migrates_and_preserves_data(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, root_dir TEXT,
          rules_dir TEXT, status TEXT DEFAULT 'pending', total_files INTEGER,
          processed_files INTEGER, success_count INTEGER, failed_count INTEGER,
          error_message TEXT, config_json TEXT, created_at TEXT, updated_at TEXT,
          started_at TEXT, finished_at TEXT);
        CREATE TABLE results (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, file_id INTEGER,
          form_type TEXT, rule_id TEXT, rule_version TEXT, rule_content_hash TEXT,
          data_json TEXT NOT NULL, status TEXT, error_level TEXT,
          source_rel_path TEXT, source_md5 TEXT, source_mtime REAL,
          snapshot_path TEXT, version INTEGER, is_deleted INTEGER DEFAULT 0,
          duplicate_of INTEGER, deleted_reason TEXT, created_at TEXT,
          updated_at TEXT);
        INSERT INTO tasks (name, root_dir, rules_dir) VALUES ('legacy', '/t', '');
        INSERT INTO results (task_id, data_json, status, is_deleted)
          VALUES (1, '{"declare_no": "SB-1"}', 'valid', 0);
        """
    )
    conn.commit()
    conn.close()

    from archive_extractor.storage.sqlite_client import SQLiteClient

    db = SQLiteClient(db_path)
    try:
        columns = [row["name"] for row in db.query("PRAGMA table_info(results)")]
        assert "arbitration_batch_id" in columns
        tables = {
            row["name"]
            for row in db.query(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for name in (
            "arbitration_batches",
            "arbitration_groups",
            "arbitration_field_records",
            "arbitration_conflicts",
            "arbitration_journal",
        ):
            assert name in tables
        # 历史数据原样保留。
        row = db.query_one("SELECT data_json FROM results WHERE id = 1")
        assert row["data_json"] == '{"declare_no": "SB-1"}'
        # 迁移后可正常写入仲裁批次。
        batch_id = db.create_arbitration_batch({
            "batch_no": "ARB-T1-TEST-0001", "task_id": 1,
            "arbiter_version": "arbiter-1.0.0",
        })
        assert db.get_arbitration_batch(batch_id)["batch_no"].endswith("0001")
    finally:
        db.close()
