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
  ├── n dedup_reports
  ├── n exports
  ├── n arbitration_batches 1 ─── n arbitration_groups
  │                                     ├── n arbitration_field_records
  │                                     └── n arbitration_conflicts
  │                          1 ─── n arbitration_journal（整批撤销日志）
  └── n rules_versions（按 rule_id + version 去重）
```

`results.file_id` 指向产生该结果的 `task_files` 行；源文件被删除时该字段置空，但结果与快照保留。`task_files`、`results`、`validations`、`traces`、`dedup_reports`、`exports` 均随任务级联删除。去重逻辑删除只更新 `results.is_deleted`，不做物理删除。

## 一点五、字段级仲裁阶段（解析校验之后、落库之前）

仲裁是独立且可回放的阶段，不改变既有去重语义与导出台账结构：

```text
扫描 → 解析 → 校验 → 【Arbiter 字段级仲裁】 → 单事务落库（快照/溯源/校验）
     → 既有去重（Deduplicator，语义不变） → 导出
```

- 同一批次内，候选按 `rule_id + dedup.primary_key` 归为同一逻辑表单组；无法计算主键的候选各自成组。
- 逐字段仲裁，优先级固定为 **非空 > 校验等级（none/info/warning/error）> 来源文件修改时间 > 稳定次序**，分段编码保证高优先级维度不会被低维度反超。
- 组内冠军候选落仲裁后的合并字段值；其余成员仍各落一条原始结果（保持“每文件一结果”的行数与溯源语义），再交给既有去重逻辑删除。
- 被淘汰值不删除，连同来源相对路径、MD5、行号、选择器、规则版本与仲裁理由写入 `arbitration_field_records`。
- 落库全程在一个 `BEGIN IMMEDIATE` 事务内，所有写动作登记 `arbitration_journal`；按批次号撤销时按日志逆序恢复，字段、溯源、快照索引、逻辑删除标记与 `task_files` 状态一起回到批次前，失败则整体回滚，不产生部分成功。
- 两个批次并发写同一逻辑表单时，后到批次不静默覆盖：差异字段写入 `arbitration_conflicts` 并附可读摘要，处理方式标记为 `recorded_pending_review`。


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
| error | TEXT | 失败原因（截断） |
| result_id | INTEGER | 最近一次成功结果 |
| updated_at | TEXT | 更新时间 |

唯一约束：`(task_id, rel_path)`。断点续跑只取 `pending`；进程退出遗留的 `processing` 在执行前会重置为 `pending`；扫描阶段发现文件消失则标记 `removed`。

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
| format | TEXT | `csv`/`json`/`xlsx`，仲裁档案为 `arbitration_csv` 等 |
| path | TEXT | 导出文件绝对路径 |
| sha256 | TEXT | 文件 SHA256 |
| created_at | TEXT | 时间 |

### 9. arbitration_batches — 仲裁批次

一次 `run` 调用对应一个仲裁批次，批次号 `ARB-T{任务}-{时间戳}-{序号}`，全局唯一。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 批次 ID |
| batch_no | TEXT UNIQUE | 可读批次号，撤销/回放/导出的入口 |
| task_id | INTEGER FK | 所属任务 |
| ruleset_signature | TEXT | 规则集整体签名 |
| arbiter_version | TEXT | 仲裁策略版本（如 `arbiter-1.0.0`） |
| rule_versions_json | TEXT | 本批次规则版本清单 |
| status | TEXT | `committed`/`rolled_back` |
| candidate_count / group_count | INTEGER | 候选数 / 分组数 |
| merged_group_count / conflict_count | INTEGER | 多候选合并组数 / 跨批冲突数 |
| summary_json | TEXT | 策略说明等摘要 |
| created_at / committed_at / rolled_back_at | TEXT | 时间 |
| rollback_reason | TEXT | 撤销原因 |

### 10. arbitration_groups — 逻辑表单分组

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 分组 ID |
| batch_id | INTEGER FK | 所属批次 |
| rule_id / form_type | TEXT | 规则与表单类型 |
| logical_key | TEXT | 主键拼接值；单候选组为空 |
| winner_result_id | INTEGER FK | 承载合并字段值的冠军结果 |
| merged_data_json | TEXT | 字段级仲裁后的合并数据 |
| original_data_json | TEXT | 冠军候选的原始提取数据 |
| candidate_count | INTEGER | 组内候选数 |
| had_conflict | INTEGER | 组内是否存在字段值冲突 |

### 11. arbitration_field_records — 字段仲裁档案（含被淘汰值）

每个候选的每个字段一行，是字段级溯源与回放的依据。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 记录 ID |
| batch_id / group_id | INTEGER FK | 批次与分组 |
| result_id | INTEGER FK | 该候选值所属结果 |
| field_path | TEXT | 字段路径，支持 `contact.email` |
| adopted | INTEGER | 1=采用，0=淘汰 |
| field_value_json | TEXT | 该候选在此字段上的值 |
| is_empty | INTEGER | 是否为空值 |
| validation_level | TEXT | 该字段校验等级 |
| source_mtime | REAL | 来源文件修改时间 |
| source_rel_path / source_md5 | TEXT | 来源相对路径与 MD5 |
| source_line | INTEGER | HTML 行号（默认值字段可空） |
| selector / selector_type | TEXT | 选择器与类型 |
| rule_version | TEXT | 规则版本 |
| rank_score | INTEGER | 仲裁分（回放依据） |
| reason | TEXT | 采用/淘汰的可读理由 |

### 12. arbitration_conflicts — 跨批并发冲突

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 冲突 ID |
| batch_id / group_id | INTEGER FK | 后到批次与分组 |
| task_id / rule_id / logical_key | | 冲突定位 |
| field_path | TEXT | 冲突字段 |
| incumbent_result_id / incoming_result_id | INTEGER | 既有/新批次结果 |
| incumbent_value_json / incoming_value_json | TEXT | 两侧值 |
| incumbent_source / incoming_source | TEXT | 两侧来源文件 |
| resolution | TEXT | 固定 `recorded_pending_review`，不静默覆盖 |
| summary | TEXT | 可读冲突摘要 |

### 13. arbitration_journal — 整批撤销日志

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 日志 ID |
| batch_id | INTEGER FK | 所属批次 |
| entity | TEXT | `results`/`traces`/`validations`/`dedup_reports`/`task_files` |
| entity_id | INTEGER | 被变更行 ID |
| operation | TEXT | `insert`/`update`/`delete`/`supersede` |
| before_json | TEXT | 变更前整行镜像，撤销时据此覆盖恢复 |

撤销按日志 `id` 逆序回放：`insert` 删除、`delete` 按镜像重插、`update` 按镜像覆盖；`supersede` 仅用于在去重之后重新确认同文件新版本的 `superseded` 标记。


## 三、索引

- `idx_results_task(task_id)`：任务结果列表。
- `idx_results_path(task_id, source_rel_path)`：同文件版本与历史。
- `idx_results_batch(arbitration_batch_id)`：按仲裁批次反查结果。
- `idx_traces_result(result_id)`：溯源链查询。
- `idx_files_task(task_id, status)`：取待处理文件。
- `idx_arb_group_batch` / `idx_arb_field_group` / `idx_arb_field_result`：仲裁分组与字段档案查询。
- `idx_arb_conflict_batch` / `idx_arb_journal_batch`：冲突与撤销日志按批次查询。

## 四、并发与一致性

- 单个 `sqlite3.Connection`，`check_same_thread=False`，所有读写由进程内 `RLock` 串行化。
- WAL 允许读写并行度更好；`synchronous=NORMAL` 在本地单机场景兼顾可靠性与批量写入性能。
- 字段校验、溯源、去重与结果写入在同一文件的持久化临界区内完成。
- 仲裁提交与整批撤销通过 `BEGIN IMMEDIATE` 显式开事务，配合进程内提交锁，保证多线程/多 `TaskRunner` 并发写同一逻辑表单时被串行化；事务内任一步失败都整体 `ROLLBACK`，不会出现半新半旧。
- 去重器在仲裁事务内回退为直查（同一连接可读到本事务未提交行），事务外仍走任务级内存缓存。
- 单文件异常只更新 `task_files.status='failed'`，不破坏已完成结果。
- 旧库通过 `init_schema` 的 `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE` 增量补列迁移，历史数据原样保留。

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
