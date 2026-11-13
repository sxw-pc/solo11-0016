# 离线字段血缘聚合

`archive_extractor/lineage/` 是一个**独立、离线、只读**的聚合模块：输入已经落库的
多个任务批次，输出一份按「表单类型 + 字段」组织的血缘视图，沿时间轴列出每个字段
在各批次里的规则版本、命中选择器、提取值与校验结论，并标出规则版本切换的断点。

## 一、约束

- **只读**：聚合只通过 `db.query` / `db.query_one` 查询 `tasks`、`results`、
  `traces`、`validations`、`rules_versions` 五张表，不执行任何写库操作，
  也不读取或修改 `snapshots/` 下的快照文件。
- **可复现**：全部产物为排序键的规范化 JSON（`sort_keys=True`、紧凑分隔符、
  不转义非 ASCII），内容不含运行时间戳；运行目录名由批次集合哈希决定，
  同一组批次无论传入顺序如何、重复运行多少次，产物逐字节一致。
- **可清理**：产物独立落在 `数据目录/lineage/` 下，可用命令整次或整目录清理，
  清理不触碰任何批次数据。
- **显式降级**：跨版本对齐证据不足时标记 `degraded` 与原因码，绝不丢弃字段。

## 二、字段归一与对齐口径

同一字段在不同规则版本下选择器可能完全不同，模块采用三层口径：

1. **恒等键（canonical_key）**：`表单类型 + "." + 点分字段路径`。
   对象字段扁平化为 `contact.email` 形式。规则版本之间字段路径不变即视为同一
   逻辑字段；字段重命名无法建立恒等关系时，不做静默合并，保留为独立键并降级。
2. **选择器签名**：取自 `traces` 表的 `selector + selector_type`。
   没有 trace 记录的字段（如纯 `default` 字段、对象容器本身）签名为空，
   对齐状态记为 `degraded`，原因 `no_trace_evidence`。
3. **批次覆盖**：某字段在某批次完全没有结果记录时，时间轴对应位置
   `present=false`、`degraded=true`，原因 `field_missing_in_batch`。

## 三、断点判定（规则变化 vs 数据变化）

对同一字段，沿时间轴比较相邻两批次的**选择器签名**与**值指纹**
（批次内全部记录值的稳定哈希）：

| 选择器 | 值 | 结论 `transition` | 断点 |
|--------|----|-------------------|------|
| 变化 | 变化 | `rule_and_data` | 是 |
| 变化 | 不变 | `rule` | 是 |
| 不变 | 变化 | `data`（纯数据变化） | 否 |
| 不变 | 不变 | 空（无变化） | 否 |
| 任一侧无 trace 证据 | — | `indeterminate` | 否，标 `degraded` |

只有证据充分、确认选择器切换的位置才标 `is_breakpoint=true`；
证据不足只降级，避免把不确定性误报成规则断点。

## 四、产物结构

```text
数据目录/lineage/
├── runs_index.json                 # 全部运行的索引（run_id、批次集合、产物哈希）
└── lineage-<批次集合哈希>/
    ├── batch_manifest.json         # 中间产物①：批次清单（结果/trace/校验/规则版本）
    ├── field_alignment.json        # 中间产物②：字段归一与跨版本对齐索引
    └── lineage_view.json           # 最终产物：按表单类型+字段的时间轴血缘视图
```

- `batch_manifest.json` 是后续两个阶段的唯一输入，判定结果落盘而非每次现算；
  三份产物各自带 `*_hash` 字段，可独立校验完整性。
- `lineage_view.json` 的 `summary` 给出批次数、表单类型、字段数、
  断点总数与降级字段数；`fields[].timeline[]` 是逐批次观测，
  `sources[]` 下钻到单条结果的取值、原始值、行号与校验明细。

## 五、命令行用法

```bash
# 聚合批次 1、2（参数顺序不影响产物）
python -m archive_extractor.cli lineage 1 2

# 指定产物目录
python -m archive_extractor.cli lineage 1 2 --output-dir /tmp/lineage_out

# 清理某次运行
python -m archive_extractor.cli lineage-clean --run-id lineage-xxxxxxxx

# 清理整个血缘产物目录
python -m archive_extractor.cli lineage-clean
```

## 六、编程接口

```python
from archive_extractor.lineage import LineageAggregator

aggregator = LineageAggregator(db, output_dir)
summary = aggregator.run([1, 2])          # 聚合并落盘
aggregator.clean(summary["run_id"])       # 清理单次运行
```
