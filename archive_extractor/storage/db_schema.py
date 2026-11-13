"""SQLite 表结构定义（设计文档见 docs/database.md）。"""

# 建表部分：CREATE TABLE IF NOT EXISTS 不会修改已存在的旧表，
# 因此新列的 ALTER 迁移必须在引用该列的索引创建之前完成。
SCHEMA_TABLES = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
CREATE TABLE IF NOT EXISTS tasks (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
 root_dir TEXT NOT NULL, rules_dir TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', total_files INTEGER DEFAULT 0,
 processed_files INTEGER DEFAULT 0, success_count INTEGER DEFAULT 0,
 failed_count INTEGER DEFAULT 0, error_message TEXT, config_json TEXT,
 created_at TEXT, updated_at TEXT, started_at TEXT, finished_at TEXT);
CREATE TABLE IF NOT EXISTS task_files (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 rel_path TEXT NOT NULL, abs_path TEXT, size INTEGER DEFAULT 0,
 mtime REAL DEFAULT 0, md5 TEXT, status TEXT DEFAULT 'pending',
 error TEXT, result_id INTEGER, updated_at TEXT,
 UNIQUE(task_id, rel_path));
CREATE TABLE IF NOT EXISTS rules_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT NOT NULL,
 version TEXT NOT NULL, content_hash TEXT NOT NULL, source_path TEXT,
 loaded_at TEXT, UNIQUE(rule_id, version));
CREATE TABLE IF NOT EXISTS results (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 file_id INTEGER REFERENCES task_files(id) ON DELETE SET NULL,
 form_type TEXT, rule_id TEXT, rule_version TEXT, rule_content_hash TEXT,
 data_json TEXT NOT NULL, status TEXT DEFAULT 'valid', error_level TEXT,
 source_rel_path TEXT, source_md5 TEXT, source_mtime REAL DEFAULT 0,
 snapshot_path TEXT, version INTEGER DEFAULT 1,
 is_deleted INTEGER DEFAULT 0, duplicate_of INTEGER, deleted_reason TEXT,
 arbitration_batch_id INTEGER,
 created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS validations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 result_id INTEGER NOT NULL REFERENCES results(id) ON DELETE CASCADE,
 level TEXT, field TEXT, message TEXT, validator TEXT);
CREATE TABLE IF NOT EXISTS traces (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 result_id INTEGER NOT NULL REFERENCES results(id) ON DELETE CASCADE,
 field TEXT, selector TEXT, selector_type TEXT, source_line INTEGER,
 raw_value TEXT, extracted_at TEXT, rule_version TEXT, source_md5 TEXT);
CREATE TABLE IF NOT EXISTS dedup_reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
 result_id INTEGER, duplicate_of INTEGER, reason TEXT,
 similarity REAL, action TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS exports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
 format TEXT, path TEXT, sha256 TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS arbitration_batches (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_no TEXT NOT NULL UNIQUE,
 task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 ruleset_signature TEXT, arbiter_version TEXT NOT NULL,
 rule_versions_json TEXT, status TEXT NOT NULL DEFAULT 'committed',
 candidate_count INTEGER DEFAULT 0, group_count INTEGER DEFAULT 0,
 merged_group_count INTEGER DEFAULT 0, conflict_count INTEGER DEFAULT 0,
 summary_json TEXT, created_at TEXT, committed_at TEXT,
 rolled_back_at TEXT, rollback_reason TEXT);
CREATE TABLE IF NOT EXISTS arbitration_groups (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_id INTEGER NOT NULL
   REFERENCES arbitration_batches(id) ON DELETE CASCADE,
 rule_id TEXT, form_type TEXT, logical_key TEXT,
 winner_result_id INTEGER, merged_data_json TEXT,
 original_data_json TEXT, candidate_count INTEGER DEFAULT 1,
 had_conflict INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS arbitration_field_records (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_id INTEGER NOT NULL
   REFERENCES arbitration_batches(id) ON DELETE CASCADE,
 group_id INTEGER NOT NULL
   REFERENCES arbitration_groups(id) ON DELETE CASCADE,
 result_id INTEGER, field_path TEXT, adopted INTEGER DEFAULT 0,
 field_value_json TEXT, is_empty INTEGER DEFAULT 0,
 validation_level TEXT, source_mtime REAL DEFAULT 0,
 source_rel_path TEXT, source_md5 TEXT, source_line INTEGER,
 selector TEXT, selector_type TEXT, rule_version TEXT,
 rank_score INTEGER DEFAULT 0, reason TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS arbitration_conflicts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_id INTEGER NOT NULL
   REFERENCES arbitration_batches(id) ON DELETE CASCADE,
 group_id INTEGER
   REFERENCES arbitration_groups(id) ON DELETE SET NULL,
 task_id INTEGER, rule_id TEXT, logical_key TEXT, field_path TEXT,
 incumbent_result_id INTEGER, incoming_result_id INTEGER,
 incumbent_value_json TEXT, incoming_value_json TEXT,
 incumbent_source TEXT, incoming_source TEXT,
 resolution TEXT, summary TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS arbitration_journal (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_id INTEGER NOT NULL
   REFERENCES arbitration_batches(id) ON DELETE CASCADE,
 entity TEXT NOT NULL, entity_id INTEGER, operation TEXT NOT NULL,
 before_json TEXT, created_at TEXT);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_results_task ON results(task_id);
CREATE INDEX IF NOT EXISTS idx_results_path ON results(task_id, source_rel_path);
CREATE INDEX IF NOT EXISTS idx_results_batch ON results(arbitration_batch_id);
CREATE INDEX IF NOT EXISTS idx_traces_result ON traces(result_id);
CREATE INDEX IF NOT EXISTS idx_files_task ON task_files(task_id, status);
CREATE INDEX IF NOT EXISTS idx_arb_group_batch ON arbitration_groups(batch_id);
CREATE INDEX IF NOT EXISTS idx_arb_field_group ON arbitration_field_records(group_id);
CREATE INDEX IF NOT EXISTS idx_arb_field_result ON arbitration_field_records(result_id);
CREATE INDEX IF NOT EXISTS idx_arb_conflict_batch ON arbitration_conflicts(batch_id);
CREATE INDEX IF NOT EXISTS idx_arb_journal_batch ON arbitration_journal(batch_id);
"""

# 兼容旧引用。
SCHEMA = SCHEMA_TABLES + SCHEMA_INDEXES

TASK_FIELDS = {
    "status", "total_files", "processed_files", "success_count",
    "failed_count", "error_message", "started_at", "finished_at",
    "updated_at",
}

# 旧库增量迁移：results 早期版本没有仲裁批次归属列。
MIGRATION_COLUMNS = {
    "results": {
        "arbitration_batch_id": "INTEGER",
    },
}
