# 常见问题排查手册

## 安装与启动

### Q1. 离线安装提示找不到包

项目按 `requirements.lock` 锁文件从 `offline_wheels/` 安装，先运行预检确认缺什么：

```bash
python scripts/install_offline.py --check
```

预检会指名缺少的 wheel 文件或哈希不符的文件。补齐方法（在可联网的准备机上）：

```bash
python scripts/lock_deps.py        # 重新解析并下载全部 wheel
```

或按报错提示用 `pip download "包名==版本" --no-deps --only-binary=:all: -d offline_wheels` 单独补齐，再把 `offline_wheels/` 整体拷贝到目标机。

离线包必须与目标机的操作系统、CPU 架构和 Python 大版本匹配。Windows 上打包的 wheel 不能直接用于 Linux；换平台请在与目标机一致的准备机上重新运行 `python scripts/lock_deps.py`。

### Q2. `python -m archive_extractor.cli` 提示模块不存在

在项目根目录执行，或先设置 `PYTHONPATH` 指向项目根目录。一键脚本会自动切换目录。不要在 `archive_extractor/` 子目录内执行。

### Q3. 端口 5001 被占用

```bash
python -m archive_extractor.cli service --port 5002
```

Linux/macOS 也可使用 `PORT=5002 sh scripts/run_service.sh`。

### Q4. 浏览器访问返回 403

服务只接受本机回环来源。请使用 `http://127.0.0.1:5001/` 或 `http://localhost:5001/`，不要通过局域网 IP、反向代理、端口映射或隧道访问。

## 扫描与编码

### Q5. 为什么某些 HTML 没有被扫描

默认只处理 `.html` / `.htm`，并跳过隐藏文件、临时文件（`.tmp`、`.swp`、`~` 等）、`__MACOSX`、`__pycache__`、`node_modules`。还可用 `--exclude-dir`、`--min-size`、`--max-size` 或任务配置中的 `scan` 进一步过滤。

### Q6. 文件编码识别失败

工具依次尝试 BOM、meta charset、UTF-8、GB18030/GBK/GB2312、Big5。不可解码文件会被标记失败但不影响其他文件。可先用编辑器确认文件是否真的为 HTML 文本，是否混入二进制内容，或转存为 UTF-8 后重试。

### Q7. GBK 文件出现乱码

确认页面没有错误的 `meta charset` 声明。若声明与实际编码冲突，优先修正源文件；工具会以声明编码优先，再回退候选编码。

## 提取规则

### Q8. 提示“未匹配到任何已配置的表单规则”

检查目标表单是否满足 `match.form_selector`，或原始文本是否包含 `marker`。规则按文件名排序，先命中者生效。可用浏览器/编辑器确认 DOM 中确实存在该 id/class。

### Q9. 字段提取为空

- `input`、`select` 通常需要 `attr: value`。
- 单选框/复选框使用 `:checked`，并确认 `checked` 属性存在。
- 对象字段的子选择器是相对容器，不是全局。
- 目标内容若在 iframe 内，本工具不跨文档提取。
- 检查字段是否配置了 `default`，空值时会使用默认值。

### Q10. 日期或数字转换失败

日期支持 `YYYY-MM-DD`、`YYYY/MM/DD`、`YYYY.MM.DD`、`YYYY年MM月DD日`，其他格式由 dateutil 尝试解析；仍失败则记为字段错误。数字支持千分位，但货币符号、单位、中文数字可能导致失败，需在规则中用 `replacements` 预处理。

### Q11. 规则修改后历史结果版本变化

这是预期行为。有效版本由业务版本与规则内容摘要共同组成，任何内容改动都会生成新版本。历史结果保留原版本字符串，不会被改写。

### Q12. 对象字段报“不支持多层嵌套”

当前对象只支持一层。请把深层结构拍平为同级字段，或拆成多个对象字段；也可在导出后做二次处理。

## 任务与去重

### Q13. 任务中断后如何续跑

重新执行同一任务即可。任务只取 `status='pending'` 的文件，已成功文件不会重复处理；进程退出遗留的 `processing` 会先重置。CLI 使用 `task-action ID resume`，Web 界面点击“继续”。

### Q14. 单文件失败会中断任务吗

不会。编码、解析、超时、校验异常都会被隔离到该文件，任务继续。失败原因记录在 `task_files.error`，可用 `errors` 命令导出。

### Q15. 重试失败文件

```bash
python -m archive_extractor.cli task-action 1 retry
python -m archive_extractor.cli task-action 1 resume
```

### Q16. 两条不同表单被误判为重复

降低模糊阈值误判风险：提高 `dedup.threshold`（如 0.98），减少 `fuzzy_fields`，或只使用 `primary_key`。被误删的记录可在 Web 结果页恢复，恢复后状态回到 `valid`。

### Q17. 为什么修改较旧的文件没有保留

默认保留源文件 `mtime` 较新的记录。若文件系统时间不可靠，会按实际 `mtime` 判断，建议先校正文件时间。

## 导出与存储

### Q18. 导出的 SHA256 如何校验

Windows：

```bat
certutil -hashfile task_1_xxx.xlsx SHA256
```

macOS / Linux：

```bash
sha256sum task_1_xxx.xlsx
```

与同名 `.sha256` 文件中的值比较。

### Q19. 如何备份与恢复

停止服务后整体复制 `data/` 目录最稳妥。仅复制数据库前先执行 `PRAGMA wal_checkpoint(TRUNCATE);`，否则可能遗漏 WAL 中的数据。恢复时保持 `archive.db`、`snapshots/`、`exports/` 一致。

### Q20. 可以删除快照吗

不建议。快照是溯源链的一部分；删除后结果仍可查询，但无法预览提取时的原始内容。需要释放空间时，优先归档旧任务并整体备份后再清理。

## 测试与性能

### Q21. 测试很慢

性能用例会创建并处理 1000 个 HTML，通常需要十几秒到几十秒。可单独运行其他测试：

```bash
python -m pytest --ignore=tests/test_performance.py
```

### Q22. 并发数越高越快吗

不一定。SQLite 写入串行，解析可并行。建议从 2–4 开始；过高并发会增加线程切换与锁等待。

### Q23. 覆盖率低于阈值

测试命令已设置 `--cov-fail-under=80`。查看 `htmlcov/index.html` 定位未覆盖分支；新增功能应同时补充单元或集成测试。

## 合规

### Q24. 可以处理网上下载的页面吗

仅在拥有合法处理权限、且文件已保存在本地时才可以。本工具不提供 URL 输入，也不会主动下载。不得处理未授权第三方数据。

### Q25. 结果可以直接归档吗

不可以直接作为最终归档结果。工具是辅助提取，所有字段、校验警告和重复处理都需要人工复核。
