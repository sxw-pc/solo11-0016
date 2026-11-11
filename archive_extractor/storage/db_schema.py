"""SQLite 表结构定义（设计文档见 docs/database.md）。"""

SCHEMA = """
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
CREATE INDEX IF NOT EXISTS idx_results_task ON results(task_id);
CREATE INDEX IF NOT EXISTS idx_results_path ON results(task_id, source_rel_path);
CREATE INDEX IF NOT EXISTS idx_traces_result ON traces(result_id);
CREATE INDEX IF NOT EXISTS idx_files_task ON task_files(task_id, status);
"""

TASK_FIELDS = {
    "status", "total_files", "processed_files", "success_count",
    "failed_count", "error_message", "started_at", "finished_at",
    "updated_at",
}
