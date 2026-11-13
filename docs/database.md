# SQLite 表结构设计

默认数据库：`data/archive.db`（`demo` 命令使用 `data/demo.db`）。数据库只保存在本地，所有写入通过 `archive_extractor/storage/sqlite_client.py` 串行化，启用外键、WAL 与 `synchronous=NORMAL`。

## 一、实体关系

```text
tasks 1 ─── n task_files
  │              │
  │              └── 0..1 results（最近一次成功结果）
  ├── n results 1 ─── n validations
  │              ├── n traces
  │              └── 0..n results（同文件历史版本，按 version）
  ├── n failure_records（隔离台账，按 rel_path 唯一）
  ├── n recheck_runs（只读复检批次记录）
  ├── n dedup_reports
  ├── n exports
  └── n rules_versions（按 rule_id + version 去重）
```

`results.file_id` 指向产生该结果的 `task_files` 行；源文件被删除时该字段置空，但结果与快照保留。`task_files`、`results`、`validations`、`traces`、`failure_records`、`recheck_runs`、`dedup_reports`、`exports` 均随任务级联删除。去重逻辑删除只更新 `results.is_deleted`，不做物理删除。

> **兼容性**：`failure_records` 与 `recheck_runs` 是 1.1 新增的**独立表**，未修改 `tasks`、`task_files`、`results` 的任何列。打开旧版数据库时会自动补建空表，历史 `task_files` 失败行仍可读取；台账查询会把没有对应台账行的历史失败记录按 `error` 文本推断原因后并入展示（标记 `legacy=true`），不会读取崩溃，也不会重复计数。

## 二、表定义

### 1. tasks — 任务批次

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增任务号 |
| name | TEXT | 任务名称 |
| root_dir | TEXT | 扫描根目录的绝对路径 |
| rules_dir | TEXT | YAML 规则目录绝对路径 |
| status | TEXT | `pending`/`running`/`paused`/`completed`/`failed`/`cancelled` |
| total_files | INTEGER | 任务文件总数 |
| processed_files | INTEGER | 已处理文件数 |
| success_count | INTEGER | 成功数 |
| failed_count | INTEGER | 失败数 |
| error_message | TEXT | 任务级错误 |
| config_json | TEXT | 并发、超时、扫描排除等 JSON 配置 |
| created_at / updated_at | TEXT | ISO 时间 |
| started_at / finished_at | TEXT | 开始/结束时间 |

### 2. task_files — 任务文件清单与断点状态

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| task_id | INTEGER FK | 所属任务 |
| rel_path | TEXT | 相对扫描根的 POSIX 路径 |
| abs_path | TEXT | 扫描时绝对路径 |
| size / mtime | INTEGER / REAL | 文件大小与修改时间 |
| md5 | TEXT | 文件 MD5，用于增量判断 |
| status | TEXT | `pending`/`processing`/`success`/`failed`/`removed` |
| error | TEXT | 失败原因（截断，兼容旧版展示） |
| result_id | INTEGER | 最近一次成功结果 |
| updated_at | TEXT | 更新时间 |

唯一约束：`(task_id, rel_path)`。断点续跑只取 `pending`；进程退出遗留的 `processing` 在执行前会重置为 `pending`；扫描阶段发现文件消失则标记 `removed`。该表结构未变，旧库可直接读取。

### 2.1 failure_records — 失败隔离台账

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| task_id | INTEGER FK | 所属任务 |
| file_id | INTEGER FK | 来源文件行，可为空 |
| rel_path | TEXT | 相对路径 |
| source_md5 | TEXT | 失败时文件 MD5 |
| category | TEXT | 分类：`encoding`/`structure`/`rule`/`processing`/`scan`/`unknown` |
| reason | TEXT | 稳定原因代码，如 `decode_failed`、`missing_form_marker` |
| stage | TEXT | 失败阶段：`scan`/`encoding`/`parse`/`match`/`process` |
| message | TEXT | 失败描述（截断） |
| occurrences | INTEGER | 正常链路重复失败次数（复检不计入） |
| status | TEXT | `isolated`/`recovered` |
| source | TEXT | `task`（入库链路）或 `recheck`（复检补建） |
| recheck_count | INTEGER | 只读复检次数 |
| last_recheck_at | TEXT | 最近复检时间 |
| created_at / updated_at | TEXT | 时间 |

唯一约束：`(task_id, rel_path)`。同一文件多次失败只累加 `occurrences`，只读复检只更新状态与次数，**不会新增记录**，因此复检后台账条数保持稳定。`recovered` 仅表示复检时可解析，不改变 `task_files.status`，也不会自动入库。

### 2.2 recheck_runs — 复检批次

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| task_id | INTEGER FK | 所属任务 |
| checked / recovered / still_failed | INTEGER | 本次检查、可解析、仍隔离数量 |
| created_at | TEXT | 时间 |

每次只读复检记一行，用于追溯复检历史；不参与任务成功/失败统计。

### 3. rules_versions — 规则版本

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| rule_id | TEXT | 规则业务 ID |
| version | TEXT | 有效版本 `版本号+内容摘要前8位` |
| content_hash | TEXT | 规则规范化 JSON 的 SHA256 前 10 位 |
| source_path | TEXT | YAML 文件路径 |
| loaded_at | TEXT | 加载时间 |

唯一约束：`(rule_id, version)`。每次任务执行前记录本次加载的规则版本。

### 4. results — 提取结果

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 结果 ID，溯源入口 |
| task_id | INTEGER FK | 任务 |
| file_id | INTEGER FK | 来源文件行 |
| form_type / rule_id | TEXT | 表单类型与规则 ID |
| rule_version | TEXT | 提取时生效版本 |
| rule_content_hash | TEXT | 规则内容摘要 |
| data_json | TEXT | 提取后的完整 JSON |
| status | TEXT | `valid`/`warning`/`invalid`/`duplicate`/`superseded` |
| error_level | TEXT | 最高校验等级 |
| source_rel_path | TEXT | 源文件相对路径 |
| source_md5 | TEXT | 源文件 MD5 |
| source_mtime | REAL | 源文件修改时间，用于去重保留较新记录 |
| snapshot_path | TEXT | 快照相对路径 |
| version | INTEGER | 同任务同文件的版本号，从 1 开始 |
| is_deleted | INTEGER | 逻辑删除标记 |
| duplicate_of | INTEGER | 重复时指向保留结果 |
| deleted_reason | TEXT | `primary_key`/`fuzzy_content`/`new_version` |
| created_at / updated_at | TEXT | 时间 |

同文件重新提取时：旧结果 `status='superseded'`、`is_deleted=1`、`deleted_reason='new_version'`；新结果版本号加 1。历史记录永久保留。

### 5. validations — 校验记录

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| result_id | INTEGER FK | 结果 |
| level | TEXT | `error`/`warning`/`info` |
| field | TEXT | 字段路径，支持 `contact.email` |
| message | TEXT | 错误原因 |
| validator | TEXT | 校验器名称，如 `required`、`phone` |

### 6. traces — 字段级溯源

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| result_id | INTEGER FK | 结果 |
| field | TEXT | 字段路径 |
| selector / selector_type | TEXT | 选择器与类型（css/xpath/regex） |
| source_line | INTEGER | HTML 行号 |
| raw_value | TEXT | 原始值（最多 500 字符） |
| extracted_at | TEXT | 提取时间 |
| rule_version | TEXT | 规则版本 |
| source_md5 | TEXT | 源文件 MD5 |

### 7. dedup_reports — 去重报告

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| task_id | INTEGER FK | 任务 |
| result_id | INTEGER | 被逻辑删除的结果 |
| duplicate_of | INTEGER | 保留的结果 |
| reason | TEXT | `primary_key` 或 `fuzzy_content` |
| similarity | REAL | 相似度，精确主键为 1.0 |
| action | TEXT | 固定 `logical_delete` |
| created_at | TEXT | 时间 |

### 8. exports — 导出记录

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| task_id | INTEGER FK | 任务 |
| format | TEXT | `csv`/`json`/`xlsx` |
| path | TEXT | 导出文件绝对路径 |
| sha256 | TEXT | 文件 SHA256 |
| created_at | TEXT | 时间 |

## 三、索引

- `idx_results_task(task_id)`：任务结果列表。
- `idx_results_path(task_id, source_rel_path)`：同文件版本与历史。
- `idx_traces_result(result_id)`：溯源链查询。
- `idx_files_task(task_id, status)`：取待处理文件。
- `idx_failures_task(task_id, status)`：隔离台账查询与复检。

## 四、并发与一致性

- 单个 `sqlite3.Connection`，`check_same_thread=False`，所有读写由进程内 `RLock` 串行化。
- WAL 允许读写并行度更好；`synchronous=NORMAL` 在本地单机场景兼顾可靠性与批量写入性能。
- 字段校验、溯源、去重与结果写入在同一文件的持久化临界区内完成。
- 单文件异常只更新 `task_files.status='failed'` 并写入一条 `failure_records`，不破坏已完成结果。
- 只读复检不进入结果写入临界区，不创建快照、不写 `results`/`validations`/`traces`/`dedup_reports`。

## 五、备份、恢复与检查

停止服务后：

```bash
# 整体备份（推荐，包含快照与导出）
cp -a data data_backup_20261111

# 仅备份数据库前先合并 WAL
sqlite3 data/archive.db "PRAGMA wal_checkpoint(TRUNCATE);"
cp data/archive.db data_backup_20261111.db
```

恢复时覆盖回原路径并保持 `snapshots/` 与数据库同步。只恢复数据库而缺少快照，历史溯源仍可看路径与 MD5，但无法预览原始内容。

常用检查：

```bash
sqlite3 data/archive.db ".tables"
sqlite3 data/archive.db "SELECT id,name,status,processed_files,total_files FROM tasks ORDER BY id DESC;"
sqlite3 data/archive.db "SELECT status,COUNT(*) FROM results GROUP BY status;"
sqlite3 data/archive.db "PRAGMA integrity_check;"
```
