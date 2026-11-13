"""CLI 辅助命令，保持 cli.py 精简。"""
from __future__ import annotations

import json
from pathlib import Path

import click

from archive_extractor.core.archiver import Archiver
from archive_extractor.core.deduplicator import Deduplicator
from archive_extractor.core.task_runner import TaskRunner
from archive_extractor.demo import clean_demo
from archive_extractor.lineage import LineageAggregator
from archive_extractor.storage.file_store import FileStore


def _runner(ctx) -> TaskRunner:
    return TaskRunner(ctx.obj["db"], ctx.obj["data_dir"],
                      timeout=ctx.obj["timeout"])


def register_extra_commands(group) -> None:
    """将辅助命令注册到主命令组。"""

    @group.command("task-action")
    @click.argument("task_id", type=int)
    @click.argument("action", type=click.Choice(
        ["pause", "resume", "cancel", "retry", "delete"]))
    @click.pass_context
    def task_action(ctx, task_id, action):
        """暂停、继续、取消、重试或删除任务。"""
        runner = _runner(ctx)
        if action == "pause":
            runner.pause(task_id)
        elif action == "resume":
            if not ctx.obj["db"].get_task(task_id):
                raise click.ClickException("任务不存在")
            ctx.obj["db"].update_task(task_id, status="pending")
            click.echo(json.dumps(runner.run(task_id), ensure_ascii=False,
                                  indent=2))
        elif action == "cancel":
            runner.cancel(task_id)
        elif action == "retry":
            click.echo(f"已重置 {runner.retry(task_id)} 个失败文件")
        else:
            runner.delete(task_id)
        click.echo(f"任务 {task_id} 已执行 {action}")

    @group.command("errors")
    @click.argument("task_id", type=int)
    @click.argument("output", type=click.Path(path_type=Path))
    @click.pass_context
    def errors_command(ctx, task_id, output):
        """导出任务错误清单 CSV。"""
        output.parent.mkdir(parents=True, exist_ok=True)
        archiver = Archiver(ctx.obj["db"], FileStore(
            Path(ctx.obj["data_dir"]) / "files"))
        click.echo(f"错误清单: {archiver.export_error_list(task_id, output)}")

    @group.command("dedup")
    @click.argument("task_id", type=int)
    @click.pass_context
    def dedup_command(ctx, task_id):
        """查看去重报告。"""
        report = Deduplicator(ctx.obj["db"]).report(task_id)
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))

    @group.command("clean-demo")
    @click.pass_context
    def clean_demo_command(ctx):
        """清理演示数据库、快照与导出。"""
        clean_demo(ctx.obj["data_dir"])
        click.echo("演示数据已清理")

    @group.command("lineage")
    @click.argument("task_ids", nargs=-1, type=int, required=True)
    @click.option("--output-dir", type=click.Path(path_type=Path), default=None,
                  help="血缘产物目录，默认 数据目录/lineage")
    @click.pass_context
    def lineage_command(ctx, task_ids, output_dir):
        """只读聚合多个任务批次的字段血缘视图与规则断点。"""
        target = output_dir or (Path(ctx.obj["data_dir"]) / "lineage")
        aggregator = LineageAggregator(ctx.obj["db"], target)
        summary = aggregator.run(list(task_ids))
        click.echo(json.dumps(summary, ensure_ascii=False, indent=2))

    @group.command("lineage-clean")
    @click.option("--run-id", default=None, help="只清理指定运行标识的产物")
    @click.option("--output-dir", type=click.Path(path_type=Path), default=None,
                  help="血缘产物目录，默认 数据目录/lineage")
    @click.pass_context
    def lineage_clean_command(ctx, run_id, output_dir):
        """清理血缘聚合中间产物（不影响批次数据与快照）。"""
        target = output_dir or (Path(ctx.obj["data_dir"]) / "lineage")
        aggregator = LineageAggregator(ctx.obj["db"], target)
        removed = aggregator.clean(run_id)
        click.echo(json.dumps({"removed": removed}, ensure_ascii=False, indent=2))
