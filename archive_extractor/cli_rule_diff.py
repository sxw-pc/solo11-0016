"""``rule-diff`` 子命令：双版本规则隔离试跑与字段级差异比对。

与主链路完全隔离：不向主库（results/validations/traces/dedup_reports/
rules_versions）和正式快照目录写入任何数据，试跑使用独立临时 SQLite 库，
命令结束（含失败、中断）后临时目录自动清理。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import click

from archive_extractor.core.rule_diff.runner import run_rule_diff, write_report
from archive_extractor.utils.common import ArchiveError


def _table_counts(db_path: Path) -> dict[str, int]:
    """记录主库各业务表行数，用于命令前后断言主库零变化。"""
    tables = (
        "tasks", "task_files", "results", "validations", "traces",
        "dedup_reports", "rules_versions", "exports",
    )
    counts: dict[str, int] = {}
    con = sqlite3.connect(str(db_path))
    try:
        for table in tables:
            counts[table] = con.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
    finally:
        con.close()
    return counts


def _print_console_summary(report: dict) -> None:
    summary = report["summary"]
    click.echo("=" * 66)
    click.echo("规则试跑差异报告（隔离试跑，主库与正式快照零写入）")
    click.echo("=" * 66)
    click.echo(
        f"扫描文件: {summary['total_files']}  受影响: "
        f"{summary['changed_files']}  无变化: {summary['unchanged_files']}"
    )
    click.echo(
        "字段差异: 新增 {field_added} / 消失 {field_removed} / "
        "值变化 {value_changed} / 校验等级变化 {level_changed}".format(**summary)
    )
    click.echo(
        f"提取状态变化: {summary['extraction_status_changed']}  "
        f"规则匹配变化: {summary['rule_match_changed']}"
    )
    cause_text = summary["changes_by_cause"] or "无"
    click.echo(f"变化根源归因: {cause_text}")
    click.echo("-" * 66)
    for item in report["affected_files"][:20]:
        click.echo(
            f"  {item['rel_path']}  MD5={item['md5']}  "
            f"{','.join(item['change_types'])}"
        )
    if len(report["affected_files"]) > 20:
        click.echo(f"  ……其余 {len(report['affected_files']) - 20} 个文件见完整报告")
    if not report["affected_files"]:
        click.echo("  两套规则对该批文件的提取结果完全一致。")
    click.echo("=" * 66)


def register_rule_diff_command(group) -> None:
    @group.command("rule-diff")
    @click.argument("scan_root", type=click.Path(exists=True, path_type=Path))
    @click.argument("old_rules", type=click.Path(path_type=Path))
    @click.argument("new_rules", type=click.Path(path_type=Path))
    @click.option(
        "--output", "-o", type=click.Path(path_type=Path), required=True,
        help="结构化差异报告输出路径（JSON）",
    )
    @click.option("--concurrency", type=int, default=1, show_default=True,
                  help="隔离试跑单任务并发度")
    @click.option("--limit", type=int, default=None,
                  help="每套规则最多试跑文件数（抽样验收用）")
    @click.option("--staging-dir", type=click.Path(path_type=Path), default=None,
                  help="临时库父目录，默认使用系统临时目录；跑完自动清理")
    @click.pass_context
    def rule_diff_command(
        ctx, scan_root, old_rules, new_rules, output,
        concurrency, limit, staging_dir,
    ):
        """对同一批静态 HTML 分别用两套规则隔离试跑并输出字段级差异报告。

        SCAN_ROOT 为静态 HTML 扫描根目录；OLD_RULES / NEW_RULES 既可以是
        规则目录，也可以是单个 YAML 规则文件（读取与版本签名逻辑同主链路）。
        """
        db_path: Path = ctx.obj["data_dir"] / "archive.db"
        before = _table_counts(db_path)
        snapshot_root = ctx.obj["data_dir"] / "files"
        snapshot_before = _snapshot_listing(snapshot_root)
        try:
            report = run_rule_diff(
                scan_root, old_rules, new_rules,
                timeout=ctx.obj["timeout"], concurrency=concurrency,
                limit=limit, staging_base=staging_dir,
            )
        except ArchiveError as exc:
            raise click.ClickException(str(exc)) from exc
        path = write_report(report, output)
        after = _table_counts(db_path)
        snapshot_after = _snapshot_listing(snapshot_root)
        report["isolation_check"] = {
            "main_db_counts_before": before,
            "main_db_counts_after": after,
            "main_db_unchanged": before == after,
            "snapshot_dir_unchanged": snapshot_before == snapshot_after,
        }
        # 隔离校验结果回写报告文件，便于离线审计
        write_report(report, output)
        if before != after or snapshot_before != snapshot_after:
            raise click.ClickException(
                "隔离校验失败：主库记录数或正式快照目录在试跑后发生变化"
            )
        _print_console_summary(report)
        click.echo(f"完整报告: {path}")
        click.echo(
            "主库表行数: 试跑前后一致 "
            f"({json.dumps(before, ensure_ascii=False)})"
        )


def _snapshot_listing(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
    )
