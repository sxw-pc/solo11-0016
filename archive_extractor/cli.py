"""档采匠命令行入口。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import click

from archive_extractor import __app_name__, __version__
from archive_extractor.config.loader import DEFAULT_RULE_DIR, load_rules
from archive_extractor.core.archiver import Archiver
from archive_extractor.core.scanner import DirectoryScanner
from archive_extractor.core.task_runner import TaskRunner
from archive_extractor.core.tracer import Tracer
from archive_extractor.cli_extras import register_extra_commands
from archive_extractor.cli_rule_diff import register_rule_diff_command
from archive_extractor.demo import print_demo, run_demo
from archive_extractor.storage.file_store import FileStore
from archive_extractor.storage.sqlite_client import SQLiteClient


def _default_home() -> Path:
    return Path(os.environ.get("ARCHIVE_EXTRACTOR_HOME", "data")).expanduser()


def _db(ctx) -> SQLiteClient:
    return ctx.obj["db"]


def _runner(ctx) -> TaskRunner:
    return TaskRunner(_db(ctx), ctx.obj["data_dir"], timeout=ctx.obj["timeout"])


def _store(ctx) -> FileStore:
    return FileStore(Path(ctx.obj["data_dir"]) / "files")


@click.group(help=f"{__app_name__} {__version__}：本地静态 HTML 表单采集提取工具")
@click.option("--data-dir", type=click.Path(path_type=Path), default=None,
              help="本地数据目录，默认 ./data")
@click.option("--timeout", type=float, default=10, show_default=True,
              help="单文件处理超时（秒）")
@click.version_option(__version__, prog_name=__app_name__)
@click.pass_context
def main(ctx, data_dir, timeout):
    """初始化 CLI 上下文。"""
    base = Path(data_dir or _default_home()).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    ctx.ensure_object(dict)
    ctx.obj.update(
        data_dir=base,
        timeout=timeout,
        db=SQLiteClient(base / "archive.db"),
    )
    ctx.call_on_close(ctx.obj["db"].close)


@main.command("init")
@click.pass_context
def init_command(ctx):
    """初始化本地数据库目录。"""
    click.echo(f"数据目录: {ctx.obj['data_dir']}")
    click.echo(f"数据库: {ctx.obj['db'].path}")


@main.command("rules")
@click.option("--rules-dir", type=click.Path(path_type=Path), default=None)
def rules_command(rules_dir):
    """列出默认或自定义规则。"""
    ruleset = load_rules(rules_dir)
    click.echo(f"规则目录: {ruleset.source_dir}  签名: {ruleset.signature}")
    for rule in ruleset.all():
        click.echo(
            f"  {rule['rule_id']:<16} {rule['name']:<10} "
            f"版本 {rule['_effective_version']}"
        )


@main.command("scan")
@click.argument("directory", type=click.Path(exists=True, path_type=Path))
@click.option("--output", type=click.Path(path_type=Path), default=None)
@click.option("--exclude-dir", multiple=True)
@click.option("--min-size", type=int, default=0)
@click.option("--max-size", type=int, default=None)
def scan_command(directory, output, exclude_dir, min_size, max_size):
    """扫描本地目录并导出 JSON 清单。"""
    scanner = DirectoryScanner(
        directory, exclude_dirs=exclude_dir, min_size=min_size, max_size=max_size
    )
    items = scanner.scan()
    if output:
        scanner.export_manifest(output, items)
    click.echo(json.dumps({
        "root": str(scanner.root), "total": len(items),
        "files": [item.rel_path for item in items],
    }, ensure_ascii=False, indent=2))


@main.command("run")
@click.argument("directory", type=click.Path(exists=True, path_type=Path))
@click.option("--name", default="本地归档采集任务")
@click.option("--rules-dir", type=click.Path(path_type=Path), default=None)
@click.option("--concurrency", type=int, default=1, show_default=True)
@click.option("--limit", type=int, default=None, help="本次最多处理文件数")
@click.pass_context
def run_command(ctx, directory, name, rules_dir, concurrency, limit):
    """创建并执行一次采集任务。"""
    ruleset = load_rules(rules_dir or DEFAULT_RULE_DIR)
    runner = _runner(ctx)
    task_id = runner.create_task(
        name, str(directory), str(ruleset.source_dir),
        {"concurrency": max(1, concurrency), "timeout": ctx.obj["timeout"]},
    )
    task = runner.run(task_id, limit=limit)
    click.echo(json.dumps(task, ensure_ascii=False, indent=2))


@main.command("tasks")
@click.option("--limit", type=int, default=20)
@click.pass_context
def tasks_command(ctx, limit):
    """查看任务历史。"""
    for task in _db(ctx).list_tasks(limit):
        click.echo(
            f"#{task['id']:<4} {task['status']:<10} "
            f"{task['processed_files']}/{task['total_files']}  {task['name']}"
        )


@main.command("status")
@click.argument("task_id", type=int)
@click.pass_context
def status_command(ctx, task_id):
    """查看任务详情与进度。"""
    task = _db(ctx).get_task(task_id)
    if not task:
        raise click.ClickException("任务不存在")
    task["counts"] = _db(ctx).refresh_task_counts(task_id)
    click.echo(json.dumps(task, ensure_ascii=False, indent=2))


@main.command("export")
@click.argument("task_id", type=int)
@click.option("--format", "fmt", type=click.Choice(["csv", "json", "xlsx", "all"]),
              default="xlsx")
@click.pass_context
def export_command(ctx, task_id, fmt):
    """导出台账（Excel/CSV/JSON）。"""
    if not _db(ctx).get_task(task_id):
        raise click.ClickException("任务不存在")
    archiver = Archiver(_db(ctx), _store(ctx))
    formats = ["xlsx", "csv", "json"] if fmt == "all" else [fmt]
    for item in formats:
        result = archiver.export(task_id, item)
        click.echo(f"{item.upper()}: {result['path']}  SHA256={result['sha256']}")


@main.command("trace")
@click.argument("result_id", type=int)
@click.pass_context
def trace_command(ctx, result_id):
    """查看结果完整溯源链。"""
    chain = Tracer(_db(ctx), _store(ctx)).chain(result_id)
    if not chain:
        raise click.ClickException("结果不存在")
    click.echo(json.dumps(chain, ensure_ascii=False, indent=2))


@main.command("demo")
@click.option("--fresh/--keep", default=True, help="是否重建演示数据库")
@click.pass_context
def demo_command(ctx, fresh):
    """使用内置 mock 归档跑通完整流程。"""
    summary = run_demo(ctx.obj["data_dir"], fresh=fresh)
    print_demo(summary)


@main.command("service")
@click.option("--port", type=int, default=5001, show_default=True)
@click.pass_context
def service_command(ctx, port):
    """启动仅监听 127.0.0.1 的本地管理界面。"""
    from archive_extractor.service.app import create_app

    app = create_app(
        db_path=ctx.obj["data_dir"] / "archive.db",
        data_dir=ctx.obj["data_dir"] / "files",
    )
    click.echo(f"本地管理界面: http://127.0.0.1:{port}/ （仅本机可访问）")
    try:
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    finally:
        app.extensions["db"].close()


register_extra_commands(main)
register_rule_diff_command(main)

if __name__ == "__main__":
    main()
