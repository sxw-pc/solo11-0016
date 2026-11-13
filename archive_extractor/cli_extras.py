"""CLI 辅助命令，保持 cli.py 精简。"""
from __future__ import annotations

import json
from pathlib import Path

import click

from archive_extractor.core.arbiter import Arbiter, ArbitrationError
from archive_extractor.core.archiver import Archiver
from archive_extractor.core.deduplicator import Deduplicator
from archive_extractor.core.task_runner import TaskRunner
from archive_extractor.demo import clean_demo
from archive_extractor.storage.file_store import FileStore


def _runner(ctx) -> TaskRunner:
    return TaskRunner(ctx.obj["db"], ctx.obj["data_dir"],
                      timeout=ctx.obj["timeout"])


def _arbiter(ctx) -> Arbiter:
    return Arbiter(ctx.obj["db"])


def _archiver(ctx) -> Archiver:
    return Archiver(
        ctx.obj["db"], FileStore(Path(ctx.obj["data_dir"]) / "files")
    )


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

    @group.command("arbitration-list")
    @click.option("--task-id", type=int, default=None, help="按任务过滤")
    @click.pass_context
    def arbitration_list(ctx, task_id):
        """列出仲裁批次。"""
        rows = ctx.obj["db"].list_arbitration_batches(task_id)
        click.echo(json.dumps(rows, ensure_ascii=False, indent=2))

    @group.command("arbitration-detail")
    @click.argument("batch_ref")
    @click.pass_context
    def arbitration_detail(ctx, batch_ref):
        """查看仲裁批次详情（字段采用/淘汰档案、冲突、撤销日志）。"""
        try:
            detail = _arbiter(ctx).batch_detail(batch_ref)
        except ArbitrationError as exc:
            raise click.ClickException(str(exc))
        click.echo(json.dumps(detail, ensure_ascii=False, indent=2))

    @group.command("arbitration-rollback")
    @click.argument("batch_ref")
    @click.option("--reason", default="manual_rollback", help="撤销原因")
    @click.pass_context
    def arbitration_rollback(ctx, batch_ref, reason):
        """按批次号或批次 ID 整体撤销仲裁，恢复批次前字段状态。"""
        try:
            result = _arbiter(ctx).rollback_batch(batch_ref, reason)
        except ArbitrationError as exc:
            raise click.ClickException(str(exc))
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))

    @group.command("arbitration-replay")
    @click.argument("batch_ref")
    @click.pass_context
    def arbitration_replay(ctx, batch_ref):
        """用仲裁档案回放字段仲裁，校验策略输出是否仍一致。"""
        try:
            result = _arbiter(ctx).replay_batch(batch_ref)
        except ArbitrationError as exc:
            raise click.ClickException(str(exc))
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))

    @group.command("arbitration-export")
    @click.argument("task_id", type=int)
    @click.option("--format", "fmt",
                  type=click.Choice(["csv", "json", "xlsx"]), default="xlsx")
    @click.option("--batch", "batch_ref", default=None,
                  help="只导出指定批次（批次号或 ID），默认导出任务全部批次")
    @click.pass_context
    def arbitration_export(ctx, task_id, fmt, batch_ref):
        """独立导出仲裁档案（被淘汰值可单独查看），不影响既有台账结构。"""
        try:
            result = _archiver(ctx).export_arbitration(task_id, fmt, batch_ref)
        except ValueError as exc:
            raise click.ClickException(str(exc))
        click.echo(
            f"{fmt.upper()} 仲裁档案: {result['path']}  "
            f"SHA256={result['sha256']} 字段记录={result['field_records']} "
            f"冲突={result['conflicts']}"
        )

    @group.command("clean-demo")
    @click.pass_context
    def clean_demo_command(ctx):
        """清理演示数据库、快照与导出。"""
        clean_demo(ctx.obj["data_dir"])
        click.echo("演示数据已清理")
