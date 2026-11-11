# 档采匠（Archive Extractor）

**版本：1.0.0** · 纯本地、离线、配置驱动的静态 HTML 表单批量采集与归档工具

## 1. 项目定位

### 核心价值

档采匠面向“已经落在本地磁盘上的静态 HTML 表单归档”，把人工逐页复制、录入、核对的工作，变成可配置、可复用、可追溯的批处理流程。它不联网、不上传、不操作浏览器，只读取被授权的本地文件，输出结构化台账。

核心能力：

- **配置驱动**：新增表单类型只改 YAML，不改核心代码。
- **全链路溯源**：结果 → 源文件 MD5 → 行号 → 选择器 → 规则版本 → 原始快照。
- **合规校验**：必填、格式、范围、枚举、跨字段逻辑校验，按错误/警告/提示分级。
- **精确与模糊去重**：逻辑删除、保留最新、可恢复、可出报告。
- **任务化处理**：并发、超时、失败隔离、断点续跑、增量重跑、历史版本保留。
- **本地台账**：CSV、JSON、Excel（五个 Sheet），自动生成 SHA256 校验文件。

### 适用场景

- 档案数字化加工中的历史申报表、登记表、公示表结构化整理。
- 企业存量申报 HTML 归档的字段抽取、质检和台账生成。
- 政务公开历史页面的本地副本整理（仅限有权处理的文件）。
- 离线内网、隔离环境中对静态表单归档做批量校验与导出。

### 边界说明

本工具**只处理**本地已存在、且使用者拥有合法处理权限的静态 `.html` / `.htm` 表单文件。它不发起网络请求，不登录、不提交、不写入任何网站，不处理在线 URL、扫描件、PDF、图片、OCR，也不提供分布式调度、多租户、云同步或公网访问。非表单类 HTML（新闻页、列表页等）不在设计目标内。

## 2. 合规声明

1. 仅可处理本人或本单位拥有合法处理权限的本地文件；严禁处理未授权第三方数据。
2. 所有解析、校验、去重、快照、导出均在本地完成，不上传任何内容，不调用外部服务。
3. 代码、默认配置与 mock 数据不包含真实密钥、Token、密码、真实机构名称、真实个人敏感信息或真实外网地址。内置数据均为程序合成。
4. 管理服务仅绑定 `127.0.0.1`，并校验请求来源为本机回环地址；不要通过反向代理、端口映射或公网隧道暴露。
5. 使用者应自行遵守档案、数据安全、个人信息保护及所在单位的保密制度。工具不替代授权审查与人工复核。

## 3. 环境要求与离线安装

### 环境要求

| 项目 | 要求 |
|------|------|
| Python | 3.9 / 3.10 / 3.11 / 3.12 |
| 操作系统 | Windows 10+、macOS、主流 Linux |
| 磁盘 | 建议预留源文件 2 倍以上空间（快照与导出） |
| 网络 | 运行、测试、演示均不需要网络 |

依赖版本固定在 `requirements.txt`：BeautifulSoup4 4.12.2、lxml 4.9.3、PyYAML 6.0.1、Click 8.1.7、Flask 3.0.0、openpyxl 3.1.2、python-dateutil 2.8.2、pytest 7.4.3、pytest-cov 4.1.0。

### 离线安装（推荐流程）

在一台与目标机同操作系统、同 Python 大版本、可联网的准备机上打包一次依赖：

```bash
python -m pip download -r requirements.txt -d offline_wheels
```

把整个项目目录与 `offline_wheels/` 一起拷贝到断网目标机，然后执行：

```bash
python -m pip install --no-index --find-links=offline_wheels -r requirements.txt
```

Windows 可将 `python` 换成 `py -3.11` 等实际解释器。若使用虚拟环境：

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
python -m pip install --no-index --find-links=offline_wheels -r requirements.txt
```

**完全离线验证**：安装完成后断开网络，依次执行 `scripts/run_demo.*`、`scripts/run_test.*`、`scripts/run_service.*`，三套流程均不依赖外网。

## 4. 快速上手：三条一键命令

所有脚本均自动切换到项目根目录，无需手工设置 `PYTHONPATH`。

### ① 演示命令

```bash
# Windows
scripts\run_demo.bat

# macOS / Linux
sh scripts/run_demo.sh
```

等价的显式命令：

```bash
python -m archive_extractor.cli demo --fresh
```

执行效果：重建 `data/demo.db`，扫描内置 80 个 HTML，完成解析、校验、去重、快照、三种台账导出，并打印任务统计、预期结果、导出路径与 SHA256。

### ② 测试命令

```bash
# Windows
scripts\run_test.bat

# macOS / Linux
sh scripts/run_test.sh
```

等价的显式命令：

```bash
python -m pytest --cov=archive_extractor --cov-branch \
  --cov-report=term-missing --cov-report=html --cov-fail-under=80
```

测试使用临时目录，不污染 `data/`；覆盖率 HTML 报告输出到 `htmlcov/index.html`。

### ③ 服务命令

```bash
# Windows
scripts\run_service.bat

# macOS / Linux
sh scripts/run_service.sh
```

等价的显式命令：

```bash
python -m archive_extractor.cli service --port 5001
```

浏览器打开 `http://127.0.0.1:5001/`。服务只监听回环地址，非本机来源返回 403。

### mock 演示预期结果

内置数据位于 `archive_extractor/mock_data/`，共 80 个 HTML：

| 指标 | 预期值 |
|------|--------|
| 扫描 HTML | 80 |
| 解析成功 / 失败 | 78 / 2（2 个损坏文件被隔离） |
| 有效结果（未删除、valid） | 58 |
| 无效结果（未删除、invalid） | 10 |
| 逻辑删除（重复） | 10 |
| 去重报告 | 10 |
| 非 HTML 附件 | 2（不进入解析队列） |

Excel 台账固定包含「批次信息、提取统计、结果明细、错误记录、溯源索引」五个 Sheet；CSV、JSON 也同时生成 `.sha256` 校验文件。数据分布与逐项预期见 [`docs/mock_data.md`](docs/mock_data.md)。

## 5. 核心概念

- **提取规则（Rule）**：一个 YAML 文件描述一类表单如何匹配、字段如何选取、如何转换与校验。加载时计算内容摘要，生成 `版本号+摘要前 8 位` 的有效版本，例如 `1.0.0+a1b2c3d4`。规则文件改动后版本自动变化。
- **任务批次（Task）**：一次扫描与处理的范围，记录扫描根目录、规则目录、并发数、配置、进度和状态。任务文件清单保存在 `task_files`，支持增量比对与断点续跑。
- **溯源链（Trace Chain）**：结果、字段级溯源、校验记录、快照、同文件历史版本的集合。每个字段记录源文件相对路径、MD5、行号、选择器、选择器类型、原始值、规则版本、提取时间。
- **去重策略**：先用 `primary_key` 做精确匹配；未命中时按 `fuzzy_fields` 计算编辑距离相似度，并取各字段相似度的最小值。达到阈值即判重。默认保留源文件修改时间较新的记录，另一条逻辑删除并写入 `dedup_reports`。
- **错误等级**：`error` 阻断为无效结果；`warning` 允许入库但标记；`info` 仅记录。字段类型转换失败按 `error` 处理。整体状态取最高等级。

## 6. 自定义规则开发指南

完整字段说明见 [`docs/yaml_schema.md`](docs/yaml_schema.md)。最小示例：

```yaml
rule_id: notice_form
name: 通知表单
version: 1.0.0
form_type: notice
match:
  form_selector: "form#noticeForm"
  marker: 'data-form-type="notice"'
fields:
  doc_no:
    label: 文号
    selector: 'input[name="doc_no"]'
    attr: value
    type: text
    required: true
    validators:
      - type: regex
        pattern: '^TZ-\d{4}-\d{3}$'
        message: 文号格式应为 TZ-年份-三位序号
  title:
    label: 标题
    selector: '//h1[@class="title"]'
    selector_type: xpath
    type: text
    required: true
  body:
    label: 正文
    selector: 'textarea[name="body"]'
    type: text
    trim: true
  tags:
    label: 标签
    selector: 'input[name="tags"]:checked'
    attr: value
    type: array
    item_type: text
    all: true
    default: []
dedup:
  primary_key: [doc_no]
  fuzzy_fields: [title]
  threshold: 0.96
```

三种提取方式：

```yaml
# CSS：最常用，适合类名、属性、表单控件
selector: '#declareForm .applicant'
selector_type: css

# XPath：适合按文本、层级或复杂条件定位
selector: '//table[@id="detail"]//tr[2]/td[3]'
selector_type: xpath

# 正则：适合从整段文本中截取，group 指定捕获组
selector: '编号[:：]\s*([A-Z0-9-]+)'
selector_type: regex
group: 1
```

常见排查：

| 现象 | 排查方向 |
|------|----------|
| 提示“未匹配到任何已配置的表单规则” | 检查 `form_selector` 是否命中，或 `marker` 是否出现在 HTML 原文 |
| 字段为空但页面有值 | 检查 `attr`（input/select 常需 `value`）、是否为 `:checked`、是否在 iframe 内 |
| 日期转换失败 | 使用 `YYYY-MM-DD`、`YYYY/MM/DD`、`YYYY.MM.DD`、`YYYY年MM月DD日`，其他格式由 dateutil 兜底 |
| 校验报“字段缺少 selector 或 default” | 每个字段必须二选一 |
| 规则改动后结果版本变化 | 正常现象；有效版本包含内容摘要 |

## 7. CLI 命令手册

全局参数置于子命令之前：

```bash
python -m archive_extractor.cli [--data-dir DIR] [--timeout 秒] 子命令 ...
```

| 命令 | 作用 | 示例 |
|------|------|------|
| `init` | 初始化数据目录与数据库 | `python -m archive_extractor.cli init` |
| `rules` | 列出规则、规则集签名与有效版本 | `python -m archive_extractor.cli rules --rules-dir ./my_rules` |
| `scan DIR` | 递归扫描并输出 JSON 清单 | `python -m archive_extractor.cli scan ./archive --output manifest.json` |
| `run DIR` | 创建并执行任务 | `python -m archive_extractor.cli run ./archive --name 批次A --concurrency 2` |
| `tasks` | 查看任务历史 | `python -m archive_extractor.cli tasks --limit 20` |
| `status ID` | 查看任务进度与统计 | `python -m archive_extractor.cli status 1` |
| `export ID` | 导出台账 | `python -m archive_extractor.cli export 1 --format all` |
| `trace RESULT_ID` | 查看完整溯源链 JSON | `python -m archive_extractor.cli trace 1` |
| `task-action ID ACTION` | 暂停/继续/取消/重试/删除 | `python -m archive_extractor.cli task-action 1 retry` |
| `errors ID OUT.csv` | 导出错误清单 | `python -m archive_extractor.cli errors 1 errors.csv` |
| `dedup ID` | 查看去重报告 | `python -m archive_extractor.cli dedup 1` |
| `demo` | 跑通内置 mock 全流程 | `python -m archive_extractor.cli demo --fresh` |
| `service` | 启动本地管理界面 | `python -m archive_extractor.cli service --port 5001` |
| `clean-demo` | 清理演示数据库、快照与导出 | `python -m archive_extractor.cli clean-demo` |

`run` 的 `--limit N` 可用于分批处理或断点续跑；`--timeout` 控制单文件超时，默认 10 秒。`export --format` 可选 `xlsx`、`csv`、`json`、`all`。

## 8. 本地管理服务使用指南

```bash
python -m archive_extractor.cli service --port 5001
```

- 访问地址：`http://127.0.0.1:5001/`。
- 任务管理：创建任务（扫描目录、规则目录、并发数、超时），启动、暂停、继续、取消、重试、删除。
- 结果查询：关键词、表单类型、错误等级、任务筛选，分页查看。
- 详情与溯源：字段数据、校验记录、历史版本、快照预览。
- 导出：CSV / JSON / XLSX 台账、错误清单；去重记录可恢复。
- 端口修改：命令行 `--port`；Linux/macOS 脚本支持环境变量 `PORT`。
- 安全：应用只绑定 `127.0.0.1`，并在请求入口校验来源地址；无登录体系，禁止对公网开放或挂到反向代理后。所有 CSS 内联在本地模板中，不引用 CDN、字体、图片或外部脚本。

## 9. 存储结构说明

默认数据根目录为 `./data`，可用 `--data-dir` 或环境变量 `ARCHIVE_EXTRACTOR_HOME` 修改。

```text
data/
├── archive.db                 # SQLite：任务、文件、结果、校验、溯源、去重、导出
├── demo.db                    # demo 命令专用数据库
└── files/
    ├── snapshots/<md5前2位>/  # 按 MD5 保存的原始文件不可变快照
    └── exports/               # CSV / JSON / XLSX 与 .sha256 校验文件
```

SQLite 使用 WAL、`synchronous=NORMAL`、外键约束和常用索引。同一任务的同文件重跑会生成新 `version`，旧版本置为 `superseded` 并逻辑删除，不物理删除。完整表结构、字段含义、关系与备份方法见 [`docs/database.md`](docs/database.md)。

备份与恢复建议：停止服务后整体复制 `data/` 目录；恢复时覆盖回同一路径。若只复制 `archive.db`，请先执行 WAL checkpoint，否则可能遗漏 `archive.db-wal` 中尚未合并的数据。不要手工删除 `snapshots/`，否则历史溯源将失去原始内容。

## 10. 性能说明

- 基线要求：1000 个 HTML 全流程（扫描 → 提取 → 校验 → 去重 → 溯源 → 入库）≤60 秒，峰值内存 ≤512MB。
- 本机实测（Windows 11、Python 3.13、并发 4、1000 个合成申报表单）：约 **16 秒**，进程峰值常驻内存约 **43MB**。测试用例 `tests/test_performance.py` 会在每次测试时重新验收 60 秒与 512MB 两条阈值。
- 单文件超时默认 10 秒，可配置；超时或异常只影响该文件，任务继续。
- 大文件优化建议：先用扫描参数 `min_size/max_size` 排除无关文件；按目录拆任务；并发数从 2–4 起步（SQLite 写入为串行，过高并发收益有限）；导出时如不需要溯源/错误明细，可关闭对应选项以减小文件体积。
- 去重为任务内两两比较。超大任务建议按批次或业务主键拆分，避免单任务内候选集无限增长。

## 11. 版本日志、贡献指南与问题反馈

### 版本日志

当前版本 **1.0.0**：完成 F1–F8 全部功能、三类默认规则、80 个合成 mock 文件、离线 CLI/Flask 界面、五 Sheet Excel 台账、SHA256 校验、75 个测试用例与性能基线。详见 [`CHANGELOG.md`](CHANGELOG.md)。

### 贡献指南

1. 保持纯离线：不得引入网络请求、CDN、遥测、云服务或真实数据。
2. 单文件不超过 300 行；新增逻辑优先放入对应 `core/`、`storage/`、`config/` 模块。
3. 新增表单类型优先通过 YAML 规则实现，不修改解析核心。
4. 提交前执行 `python -m pytest`、flake8 检查，并确认测试使用临时目录。
5. 测试数据必须为合成数据；提交内容不得包含真实机构、人员、证件号或密钥。
6. 变更数据库结构时同步更新 `docs/database.md` 与迁移说明。

### 问题反馈模板

反馈请包含：版本号、Python 与操作系统、执行命令、脱敏后的错误输出、任务 ID、是否可复现、涉及规则片段（删除敏感值）。完整模板见 [`docs/feedback_template.md`](docs/feedback_template.md)，常见故障先查 [`docs/faq.md`](docs/faq.md)。本项目不提供任何真实外部链接。

## 12. 免责声明

本工具仅为辅助提取工具，不保证提取结果绝对准确、完整或满足特定业务/法律要求。所有结果必须由人工复核后才能用于归档、申报、公开或其他正式用途。使用者应自行确认文件来源合法、处理权限充分，并自行承担数据处理、存储、导出与使用过程中的合规责任。作者与贡献者不对因使用本工具产生的任何直接或间接损失承担责任。
