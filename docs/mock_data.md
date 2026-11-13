# mock 数据预期结果说明

内置数据位于 `archive_extractor/mock_data/`，全部由程序合成，不含真实机构、姓名、电话、证件号或地址。数据用于离线演示、集成测试与性能基线，不需要任何网络资源。

## 一、目录与文件分布

```text
mock_data/
├── 2023企业申报归档/
│   ├── normal/        30 个正常申报表（含 1 个 GBK 编码样本）
│   ├── abnormal/      10 个异常表（5 个缺字段、5 个格式值错误）
│   ├── broken/        2 个损坏 HTML（编码/结构不可解析）
│   ├── duplicate/     10 个重复内容表单（主键重复）
│   └── attachments/   2 个非 HTML 附件（不进入解析队列）
├── 2024政务公示归档/
│   ├── normal/        20 个正常公示表
│   └── nested/        5 个嵌套表格公示表 + 3 个登记表
└── mock_manifest.json 机器可读的预期结果
```

HTML 合计 **80** 个：

- 2023 企业申报归档 52 个：30 个正常表 + 10 个异常表（5 缺字段、5 格式错误）+ 10 个重复表 + 2 个损坏文件。其中可参与规则解析的申报表单为 50 个，损坏文件 2 个。
- 2024 政务公示归档 28 个：20 个正常公示表 + 5 个嵌套公示表 + 3 个登记表。
- 附件 2 个，非 HTML，不进入解析队列。

准确口径以 `mock_manifest.json` 的 `expected` 为准：

| 键 | 值 | 含义 |
|----|----|------|
| `total_html_files` | 80 | 扫描到的 HTML 总数 |
| `declare_forms` | 50 | 申报类表单总数 |
| `public_forms` | 25 | 公示类表单总数 |
| `register_forms` | 3 | 登记类表单总数 |
| `normal_declare` | 30 | 正常申报表 |
| `abnormal_missing_fields` | 5 | 缺少必填字段 |
| `abnormal_format_values` | 5 | 格式/类型错误 |
| `broken_files` | 2 | 无法编码或解析 |
| `duplicate_files` | 10 | 重复表单 |
| `attachments_non_html` | 2 | 非 HTML 附件 |
| `parse_successes` | 78 | 成功解析 |
| `parse_failures` | 2 | 损坏文件失败 |
| `dedup_reports` | 10 | 去重报告 |
| `active_valid_results` | 58 | 未删除且 `valid` |
| `active_invalid_results` | 10 | 未删除且 `invalid` |
| `logically_deleted_results` | 10 | 重复被逻辑删除 |

> 目录中的 52 个 HTML 包含 2 个损坏文件；`declare_forms=50` 指可解析的申报表单。公示 25 = 20 正常 + 5 嵌套；登记 3 个位于公示归档的嵌套目录。

## 二、演示预期

执行 `python -m archive_extractor.cli demo --fresh` 后：

- 任务状态 `completed`。
- 总文件 80，成功 78，失败 2；失败原因为文件编码/HTML 无法解析，任务不中断。
- 隔离台账按原因聚合为「编码类 / 文件编码无法识别」2 个文件；只读复检后仍为 2 条记录。
- 10 条重复记录被逻辑删除，保留较新 `mtime` 的记录，生成 10 条去重报告。
- 未删除结果中，58 条 `valid`、10 条 `invalid`。
- 每个成功结果都有快照、字段级溯源与规则版本。
- 导出 `xlsx`、`csv`、`json` 各一份，并生成同名 `.sha256` 文件。

由于 `demo` 会重建 `data/demo.db`，任务 ID 通常为 1，但以控制台输出为准。

### 隔离台账与只读复检验收

```bash
# 查看隔离台账（按原因分类 + 批次/单文件判断）
python -m archive_extractor.cli failures 1

# 只读复检：不重新入库、不去重、不新增失败记录
python -m archive_extractor.cli recheck 1

# 复检后再次查看：隔离文件仍应只有 2 条，原因仍为 decode_failed
python -m archive_extractor.cli failures 1
```

手工验收结构类：把任一正常文件的 `<form ... data-form-type="declare">` 改为
`<div id="declareForm">` 并同步闭合标签，重新执行任务后其失败原因应为
`missing_form_marker`（结构类），而不是编码类。

## 三、异常样本说明

- **缺字段**：删除部分必填项，校验器产生 `required` 错误，结果为 `invalid`。
- **格式错误**：金额、日期、电话、信用代码等填入非法值，产生格式/范围错误。
- **损坏文件**：包含不可解码字节或极短残缺 HTML，读取或解析阶段抛出异常，文件标记 `failed`。
- **重复文件**：主键相同，文件修改时间不同；执行去重后保留较新一条，另一条 `status=duplicate`、`is_deleted=1`。
- **GBK 样本**：验证编码自动识别；解析结果与 UTF-8 样本一致。
- **嵌套目录**：验证递归扫描、相对路径与嵌套表格对象提取。

## 四、验证方式

```bash
# 全量测试（含 mock 集成与性能）
python -m pytest

# 只跑 mock 全链路用例
python -m pytest tests/test_task_runner.py::test_builtin_mock_manifest_end_to_end -q

# 查看任务与结果统计
python -m archive_extractor.cli demo
python -m archive_extractor.cli tasks
python -m archive_extractor.cli status 1
```

若修改了 mock 数据，需要同步更新 `mock_manifest.json`、`tests/test_task_runner.py` 中的断言与本文档，否则验收会失败。生成逻辑不依赖网络或随机种子；新增样本必须继续使用合成数据。

## 五、性能基线样本

性能测试在临时目录复制 1000 份正常申报表，仅替换编号，验证全流程（扫描、解析、校验、去重、溯源、入库）耗时 ≤60 秒、峰值内存 ≤512MB。测试文件不写入项目目录，结束后由 pytest 清理。
