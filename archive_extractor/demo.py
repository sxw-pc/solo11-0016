"""内置 mock 归档的一键演示流程。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from archive_extractor.config.loader import load_rules
from archive_extractor.core.archiver import Archiver
from archive_extractor.core.scanner import DirectoryScanner
from archive_extractor.core.task_runner import TaskRunner
from archive_extractor.storage.file_store import FileStore
from archive_extractor.storage.sqlite_client import SQLiteClient

MOCK_DIR = Path(__file__).resolve().parent / "mock_data"


def _expected() -> dict:
    manifest = MOCK_DIR / "mock_manifest.json"
    if not manifest.exists():
        return {}
    return json.loads(manifest.read_text(encoding="utf-8")).get("expected", {})


def run_demo(data_dir: Path | str, fresh: bool = True) -> dict:
    """执行扫描、提取、校验、去重、溯源、导出，返回摘要。"""
    base = Path(data_dir).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / "demo.db"
    if fresh and db_path.exists():
        db_path.unlink()
    db = SQLiteClient(db_path)
    try:
        ruleset = load_rules()
        runner = TaskRunner(db, base / "demo_files", timeout=10)
        task_id = runner.create_task(
            "内置 mock 归档演示",
            str(MOCK_DIR),
            str(ruleset.source_dir),
            {"concurrency": 2, "timeout": 10},
        )
        task = runner.run(task_id)
        store = FileStore(base / "demo_files")
        archiver = Archiver(db, store)
        exports = {
            fmt: archiver.export(
                task_id, fmt,
                options={"include_trace": True, "include_errors": True},
            )
            for fmt in ("xlsx", "csv", "json")
        }
        items = DirectoryScanner(MOCK_DIR).scan()
        summary = {
            "task_id": task_id,
            "status": task["status"],
            "scanned_html": len(items),
            "task": task,
            "expected": _expected(),
            "exports": exports,
            "errors": db.query(
                """SELECT COUNT(*) AS amount FROM validations v JOIN results r
                   ON r.id = v.result_id WHERE r.task_id = ? AND v.level = 'error'""",
                (task_id,),
            )[0]["amount"],
            "dedup": db.query(
                "SELECT COUNT(*) AS amount FROM dedup_reports WHERE task_id = ?",
                (task_id,),
            )[0]["amount"],
        }
        return summary
    finally:
        db.close()


def print_demo(summary: dict) -> None:
    """友好的控制台摘要。"""
    task = summary["task"]
    print("=" * 66)
    print("档采匠 · 本地静态 HTML 表单采集演示")
    print("=" * 66)
    print(f"任务编号: {summary['task_id']}    状态: {task['status']}")
    print(f"扫描 HTML: {summary['scanned_html']} 个")
    print(
        f"进度: 总文件 {task['total_files']} / 已处理 {task['processed_files']} "
        f"/ 成功 {task['success_count']} / 失败 {task['failed_count']}"
    )
    print(f"校验错误: {summary['errors']} 条    去重记录: {summary['dedup']} 条")
    print("-" * 66)
    expected = summary.get("expected", {})
    if expected:
        print("mock 预期: " + json.dumps(expected, ensure_ascii=False))
    print("导出文件（均已生成 SHA256 校验文件）:")
    for fmt, info in summary["exports"].items():
        print(f"  [{fmt.upper()}] {info['path']}  ({info['records']} 条)")
        print(f"         SHA256 {info['sha256']}")
    print("-" * 66)
    print("溯源演示: python -m archive_extractor.cli trace 1")
    print("管理界面: python -m archive_extractor.cli service")
    print("=" * 66)


def clean_demo(data_dir: Path | str) -> None:
    """清理演示数据库与导出目录（仅供显式调用）。"""
    base = Path(data_dir).expanduser().resolve()
    for name in ("demo.db", "demo_files"):
        target = base / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
