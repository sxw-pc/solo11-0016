"""失败分类、隔离台账、只读复检与旧库兼容测试。"""
import sqlite3
from pathlib import Path

import pytest

from archive_extractor.config.loader import DEFAULT_RULE_DIR, load_rules
from archive_extractor.core.failures import (
    build_failure,
    classify_exception,
    collect_isolated,
    infer_legacy_reason,
    summarize,
)
from archive_extractor.core.parser import FormExtractor
from archive_extractor.core.recheck import Rechecker
from archive_extractor.core.task_runner import TaskRunner
from archive_extractor.storage.sqlite_client import SQLiteClient
from archive_extractor.utils.common import EncodingError, ParseError

MOCK = Path(__file__).resolve().parents[1] / "archive_extractor" / "mock_data"
DECLARE = MOCK / "2023企业申报归档/normal/declare_001.html"


def _task(runner, root, name="失败台账"):
    return runner.create_task(
        name, str(root), str(DEFAULT_RULE_DIR), {"concurrency": 1}
    )


@pytest.fixture
def normal_text():
    return DECLARE.read_text(encoding="utf-8")


def test_classify_exception_is_stable_and_text_independent():
    assert classify_exception(
        EncodingError("任意文案", reason="decode_failed")
    ).category == "encoding"
    missing = classify_exception(
        ParseError("x", reason="missing_form_marker", stage="match")
    )
    assert (missing.category, missing.stage, missing.reason) == (
        "structure", "match", "missing_form_marker"
    )
    unmatched = classify_exception(ParseError("未匹配到任何已配置的表单规则"))
    assert unmatched.reason == "malformed_html"  # 未带 reason 时按默认结构类
    assert classify_exception(TimeoutError("x")).reason == "timeout"
    assert classify_exception(RuntimeError("x")).category == "processing"


def test_parser_distinguishes_missing_form_from_unmatched_rule(ruleset):
    extractor = FormExtractor(ruleset)
    no_form = "<html><body><div>没有表单</div></body></html>"
    with pytest.raises(ParseError) as excinfo:
        extractor.extract(no_form)
    assert excinfo.value.reason == "missing_form_marker"
    news_page = "<html><body><form id='other'></form></body></html>"
    with pytest.raises(ParseError) as excinfo:
        extractor.extract(news_page)
    assert excinfo.value.reason == "rule_unmatched"


def test_mock_batch_isolation_and_stable_recheck(tmp_path, db, data_dir, ruleset):
    runner = TaskRunner(db, data_dir / "files", timeout=10)
    task_id = runner.create_task(
        "mock 全量", str(MOCK), str(ruleset.source_dir), {"concurrency": 2}
    )
    task = runner.run(task_id)
    assert task["success_count"] == 78 and task["failed_count"] == 2

    first = runner.failures(task_id)
    assert first["summary"]["isolated_count"] == 2
    assert len(first["records"]) == 2
    assert {row["category"] for row in first["records"]} == {"encoding"}
    assert {row["reason"] for row in first["records"]} == {"decode_failed"}

    for _ in range(2):
        result = runner.recheck(task_id)
        assert result["checked"] == 2
        assert result["still_failed"] == 2 and result["recovered"] == 0
    after = runner.failures(task_id)
    assert len(after["records"]) == 2
    assert {row["reason"] for row in after["records"]} == {"decode_failed"}
    assert all(row["recheck_count"] == 2 for row in after["records"])
    assert len(after["rechecks"]) == 2
    # 复检不得产生新结果：仍是 78 个成功文件的结果行
    assert db.count_results({"task_id": task_id}, include_deleted=True) == 78


def test_broken_marker_classifies_as_structure(tmp_path, db, data_dir, normal_text):
    root = tmp_path / "archive"
    root.mkdir()
    broken = normal_text.replace(
        '<form id="declareForm" data-form-type="declare">',
        '<div id="declareForm">',
    ).replace("</form>", "</div>")
    (root / "no_form.html").write_text(broken, encoding="utf-8")
    runner = TaskRunner(db, data_dir / "files")
    task_id = _task(runner, root)
    runner.run(task_id)
    records = runner.failures(task_id)["records"]
    assert len(records) == 1
    assert records[0]["reason"] == "missing_form_marker"
    assert records[0]["category"] == "structure"
    assert records[0]["stage"] == "match"

    # 复检仍归类为结构类，且不新增记录
    result = runner.recheck(task_id)
    assert result["still_failed"] == 1
    records = runner.failures(task_id)["records"]
    assert len(records) == 1 and records[0]["reason"] == "missing_form_marker"


def test_recheck_is_read_only_and_detects_recovery(tmp_path, db, data_dir,
                                                   normal_text):
    root = tmp_path / "archive"
    root.mkdir()
    broken = root / "broken.html"
    broken.write_bytes(b"\xff\xff\x80\x81")
    (root / "ok.html").write_text(normal_text, encoding="utf-8")
    runner = TaskRunner(db, data_dir / "files")
    task_id = _task(runner, root)
    runner.run(task_id)
    before = db.count_results({"task_id": task_id}, include_deleted=True)
    assert before == 1

    # 修复文件后复检：应识别为可解析，但不重新入库、不去重、不新增结果
    broken.write_text(normal_text.replace("SB-2023-0001", "SB-2023-0777"),
                      encoding="utf-8")
    result = runner.recheck(task_id)
    assert result["recovered"] == 1 and result["still_failed"] == 0
    assert db.count_results({"task_id": task_id}, include_deleted=True) == before
    records = runner.failures(task_id)["records"]
    assert len(records) == 1
    assert records[0]["recheck_status"] == "recovered"
    assert records[0]["reason"] == "decode_failed"  # 原因分类稳定
    assert db.get_task_file(task_id, "broken.html")["status"] == "failed"


def test_retry_success_clears_isolation(tmp_path, db, data_dir, normal_text):
    root = tmp_path / "archive"
    root.mkdir()
    broken = root / "broken.html"
    broken.write_bytes(b"\xff\xff\x80\x81")
    runner = TaskRunner(db, data_dir / "files")
    task_id = _task(runner, root)
    runner.run(task_id)
    assert len(runner.failures(task_id)["records"]) == 1
    broken.write_text(normal_text, encoding="utf-8")
    assert runner.retry(task_id) == 1
    task = runner.run(task_id)
    assert task["failed_count"] == 0
    assert runner.failures(task_id)["records"] == []


def test_repeated_failure_does_not_duplicate_ledger(tmp_path, db, data_dir):
    root = tmp_path / "archive"
    root.mkdir()
    (root / "broken.html").write_bytes(b"\xff\xff\x80\x81")
    runner = TaskRunner(db, data_dir / "files")
    task_id = _task(runner, root)
    for _ in range(3):
        runner.run(task_id)
        runner.retry(task_id)
    records = runner.failures(task_id)["records"]
    assert len(records) == 1
    assert records[0]["occurrences"] >= 2


def test_summary_flags_rule_set_scope(tmp_path, db, data_dir):
    root = tmp_path / "archive"
    root.mkdir()
    for index in range(6):
        (root / f"page_{index}.html").write_text(
            f"<html><body><form id='other'><div>{index}</div></form></body></html>",
            encoding="utf-8",
        )
    runner = TaskRunner(db, data_dir / "files")
    task_id = _task(runner, root)
    runner.run(task_id)
    assessment = runner.failures(task_id)["summary"]["assessment"]
    assert assessment["scope"] == "rule_set"
    assert assessment["category"] == "rule"
    assert assessment["count"] == 6


def test_summary_flags_individual_files(tmp_path, db, data_dir, normal_text):
    root = tmp_path / "archive"
    root.mkdir()
    (root / "broken.html").write_bytes(b"\xff\xff\x80\x81")
    for index in range(9):
        (root / f"ok_{index}.html").write_text(
            normal_text.replace("SB-2023-0001", f"SB-2023-00{index + 10}"),
            encoding="utf-8",
        )
    runner = TaskRunner(db, data_dir / "files")
    task_id = _task(runner, root)
    runner.run(task_id)
    assessment = runner.failures(task_id)["summary"]["assessment"]
    assert assessment["scope"] == "file"


def test_legacy_database_rows_are_readable(tmp_path, data_dir, normal_text):
    db_path = data_dir / "legacy.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
         root_dir TEXT NOT NULL, rules_dir TEXT NOT NULL, status TEXT NOT NULL,
         total_files INTEGER DEFAULT 0, processed_files INTEGER DEFAULT 0,
         success_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0,
         error_message TEXT, config_json TEXT, created_at TEXT, updated_at TEXT,
         started_at TEXT, finished_at TEXT);
        CREATE TABLE task_files (id INTEGER PRIMARY KEY AUTOINCREMENT,
         task_id INTEGER NOT NULL, rel_path TEXT NOT NULL, abs_path TEXT,
         size INTEGER DEFAULT 0, mtime REAL DEFAULT 0, md5 TEXT,
         status TEXT DEFAULT 'pending', error TEXT, result_id INTEGER,
         updated_at TEXT, UNIQUE(task_id, rel_path));
        INSERT INTO tasks (name, root_dir, rules_dir, status, total_files,
         processed_files, success_count, failed_count, created_at, updated_at)
         VALUES ('旧批次', '/old', '/rules', 'completed', 2, 2, 0, 2,
                 '2026-01-01', '2026-01-01');
        INSERT INTO task_files (task_id, rel_path, abs_path, size, md5, status,
         error, updated_at) VALUES (1, 'bad/enc.html', '/old/bad/enc.html', 10,
         'aaa', 'failed', '文件编码无法识别，已尝试 UTF-8/GBK/GB2312/Big5',
         '2026-01-01');
        INSERT INTO task_files (task_id, rel_path, abs_path, size, md5, status,
         error, updated_at) VALUES (1, 'bad/rule.html', '/old/bad/rule.html', 10,
         'bbb', 'failed', '未匹配到任何已配置的表单规则', '2026-01-01');
        """
    )
    raw.commit()
    raw.close()
    db = SQLiteClient(db_path)  # 新表自动补建，旧表保持原样
    try:
        records = collect_isolated(db, 1)
        assert len(records) == 2
        assert {row["reason"] for row in records} == {
            "decode_failed", "rule_unmatched"
        }
        assert all(row["legacy"] for row in records)
        summary = summarize(records, 2)
        assert summary["isolated_count"] == 2
        runner = TaskRunner(db, data_dir / "files")
        payload = runner.failures(1)
        assert len(payload["records"]) == 2
    finally:
        db.close()


def test_legacy_reason_inference():
    assert infer_legacy_reason("文件编码无法识别") == "decode_failed"
    assert infer_legacy_reason("处理超时（10 秒）") == "timeout"
    assert infer_legacy_reason("未匹配到任何已配置的表单规则") == "rule_unmatched"
    assert infer_legacy_reason("完全未知的旧错误") == "legacy_unclassified"


def test_legacy_record_recheck_creates_single_row(tmp_path, data_dir):
    db_path = data_dir / "legacy.db"
    root = tmp_path / "archive"
    root.mkdir()
    (root / "broken.html").write_bytes(b"\xff\xff\x80\x81")
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
         root_dir TEXT NOT NULL, rules_dir TEXT NOT NULL, status TEXT NOT NULL,
         total_files INTEGER DEFAULT 0, processed_files INTEGER DEFAULT 0,
         success_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0,
         error_message TEXT, config_json TEXT, created_at TEXT, updated_at TEXT,
         started_at TEXT, finished_at TEXT);
        CREATE TABLE task_files (id INTEGER PRIMARY KEY AUTOINCREMENT,
         task_id INTEGER NOT NULL, rel_path TEXT NOT NULL, abs_path TEXT,
         size INTEGER DEFAULT 0, mtime REAL DEFAULT 0, md5 TEXT,
         status TEXT DEFAULT 'pending', error TEXT, result_id INTEGER,
         updated_at TEXT, UNIQUE(task_id, rel_path));
        INSERT INTO tasks (name, root_dir, rules_dir, status, total_files,
         processed_files, success_count, failed_count, created_at, updated_at)
         VALUES ('旧批次', '', '', 'completed', 1, 1, 0, 1,
                 '2026-01-01', '2026-01-01');
        """
    )
    raw.commit()
    raw.close()
    db = SQLiteClient(db_path)
    try:
        db.execute(
            "UPDATE tasks SET root_dir = ?, rules_dir = ? WHERE id = 1",
            (str(root), str(DEFAULT_RULE_DIR)),
        )
        db.execute(
            """INSERT INTO task_files (task_id, rel_path, abs_path, size, md5,
               status, error, updated_at) VALUES
               (1, 'broken.html', ?, 4, 'aaa', 'failed', '文件编码无法识别',
                '2026-01-01')""",
            (str(root / "broken.html"),),
        )
        ruleset = load_rules()
        rechecker = Rechecker(db, ruleset)
        for _ in range(3):
            result = rechecker.run(1)
            assert result["still_failed"] == 1
            assert len(db.failure_rows(1)) == 1
        records = collect_isolated(db, 1)
        assert len(records) == 1
        assert records[0]["recheck_count"] == 3
        assert records[0]["legacy"] is False
    finally:
        db.close()


def test_recheck_with_bad_rules_raises_runtime(tmp_path, db, data_dir):
    root = tmp_path / "archive"
    root.mkdir()
    (root / "broken.html").write_bytes(b"\xff\xff\x80\x81")
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task(
        "坏规则", str(root), str(tmp_path / "missing_rules"), {"concurrency": 1}
    )
    runner.prepare(task_id)
    db.update_task_file(
        db.get_task_file(task_id, "broken.html")["id"], status="failed",
        error="文件编码无法识别",
    )
    with pytest.raises(RuntimeError):
        runner.recheck(task_id)


def test_rechecker_direct_parse_failure(tmp_path, ruleset):
    class FakeDb:
        def record_failure(self, *args, **kwargs):
            return 1

    rechecker = Rechecker(FakeDb(), ruleset, timeout=5)
    target = tmp_path / "missing.html"
    failure = rechecker._parse(target)
    assert failure is not None and failure.reason == "file_unreadable"
    assert build_failure("decode_failed", "x").category_label == "编码类"
