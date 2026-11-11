"""任务执行、异常隔离、断点续跑与增量版本测试。"""
import json
import time
from pathlib import Path

import pytest

from archive_extractor.config.loader import DEFAULT_RULE_DIR
from archive_extractor.core.parser import FormExtractor
from archive_extractor.core.task_runner import TaskRunner

MOCK = Path(__file__).resolve().parents[1] / "archive_extractor" / "mock_data"


def make_archive(tmp_path, sample: str, count: int = 1) -> Path:
    root = tmp_path / "archive"
    root.mkdir(exist_ok=True)
    for index in range(1, count + 1):
        text = sample.replace("0001", f"{index:04d}")
        (root / f"form_{index:03d}.html").write_text(text, encoding="utf-8")
    return root


def test_full_mock_pipeline_and_expected_counts(db, data_dir, sample_declare_text):
    root = make_archive(data_dir, sample_declare_text, 1)
    task_id = db.create_task("单一任务", str(root), str(DEFAULT_RULE_DIR),
                             {"concurrency": 1})
    runner = TaskRunner(db, data_dir / "files", timeout=10)
    assert runner.prepare(task_id)["new"] == 1
    task = runner.run(task_id)
    assert task["status"] == "completed"
    assert task["success_count"] == 1
    result = db.list_results({"task_id": task_id})[0]
    assert result["source_md5"]
    assert db.query(
        "SELECT COUNT(*) AS n FROM traces WHERE result_id = ?", (result["id"],)
    )[0]["n"] > 10
    snapshot = data_dir / "files" / result["snapshot_path"]
    assert snapshot.exists()


def test_builtin_mock_manifest_end_to_end(db, data_dir, ruleset):
    runner = TaskRunner(db, data_dir / "files", timeout=10)
    task_id = runner.create_task(
        "mock 全量", str(MOCK), str(ruleset.source_dir), {"concurrency": 2}
    )
    task = runner.run(task_id)
    assert task["status"] == "completed"
    assert task["total_files"] == 80
    assert task["success_count"] == 78 and task["failed_count"] == 2
    counts = {
        "valid": db.query(
            """SELECT COUNT(*) AS n FROM results WHERE task_id = ?
               AND is_deleted = 0 AND status = 'valid'""", (task_id,)
        )[0]["n"],
        "invalid": db.query(
            """SELECT COUNT(*) AS n FROM results WHERE task_id = ?
               AND is_deleted = 0 AND status = 'invalid'""", (task_id,)
        )[0]["n"],
        "duplicate": db.query(
            "SELECT COUNT(*) AS n FROM results WHERE task_id = ? AND is_deleted = 1",
            (task_id,)
        )[0]["n"],
        "dedup": db.query(
            "SELECT COUNT(*) AS n FROM dedup_reports WHERE task_id = ?", (task_id,)
        )[0]["n"],
    }
    assert counts == {"valid": 58, "invalid": 10, "duplicate": 10, "dedup": 10}


def test_breakpoint_resume(tmp_path, db, data_dir, sample_declare_text):
    root = make_archive(tmp_path, sample_declare_text, 5)
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task("续跑", str(root), str(DEFAULT_RULE_DIR))
    first = runner.run(task_id, limit=2)
    assert first["status"] == "paused"
    assert db.count_results({"task_id": task_id}, include_deleted=True) == 2
    second = runner.run(task_id)
    assert second["status"] == "completed"
    assert second["success_count"] == 5
    assert db.count_results({"task_id": task_id}) == 5


def test_failure_isolation_and_retry(tmp_path, db, data_dir, sample_declare_text):
    root = make_archive(tmp_path, sample_declare_text, 1)
    broken = root / "broken.html"
    broken.write_bytes(b"\xff\xff\x80\x81")
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task("异常隔离", str(root), str(DEFAULT_RULE_DIR))
    task = runner.run(task_id)
    assert task["status"] == "completed"
    assert task["success_count"] == 1 and task["failed_count"] == 1
    failed = db.get_task_file(task_id, "broken.html")
    assert "编码" in failed["error"]
    broken.write_text(sample_declare_text.replace("0001", "0009"), encoding="utf-8")
    assert runner.retry(task_id) == 1
    task = runner.run(task_id)
    assert task["success_count"] == 2 and task["failed_count"] == 0


def test_incremental_reprocess_keeps_history(
        tmp_path, db, data_dir, sample_declare_text):
    root = make_archive(tmp_path, sample_declare_text, 1)
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task("增量", str(root), str(DEFAULT_RULE_DIR))
    runner.run(task_id)
    path = root / "form_001.html"
    path.write_text(
        sample_declare_text.replace("示例申报企业0001", "示例申报企业0099"),
        encoding="utf-8",
    )
    summary = runner.prepare(task_id)
    assert summary["changed"] == 1
    task = runner.run(task_id)
    assert task["status"] == "completed"
    rows = db.query(
        "SELECT id, version, status FROM results WHERE task_id = ? ORDER BY version",
        (task_id,),
    )
    assert [row["version"] for row in rows] == [1, 2]
    assert rows[0]["status"] == "superseded"
    assert rows[1]["status"] == "valid"
    assert db.count_results({"task_id": task_id}, include_deleted=True) == 2


def test_removed_files_are_tagged(tmp_path, db, data_dir, sample_declare_text):
    root = make_archive(tmp_path, sample_declare_text, 2)
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task("删除检测", str(root), str(DEFAULT_RULE_DIR))
    (root / "form_002.html").unlink()
    summary = runner.prepare(task_id)
    assert summary["removed"] == 1
    assert db.get_task_file(task_id, "form_002.html")["status"] == "removed"


def test_timeout_marks_file_failed(monkeypatch, tmp_path, db, data_dir,
                                   sample_declare_text):
    root = make_archive(tmp_path, sample_declare_text, 1)
    runner = TaskRunner(db, data_dir / "files", timeout=0.05)

    def slow_extract(self, raw_text, source_hint=""):
        time.sleep(0.2)
        return {"data": {}}

    monkeypatch.setattr(FormExtractor, "extract", slow_extract)
    task_id = runner.create_task("超时", str(root), str(DEFAULT_RULE_DIR))
    task = runner.run(task_id)
    assert task["failed_count"] == 1
    assert "超时" in db.get_task_file(task_id, "form_001.html")["error"]


def test_task_actions_and_errors(tmp_path, db, data_dir, sample_declare_text):
    root = make_archive(tmp_path, sample_declare_text, 1)
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task("操作", str(root), str(DEFAULT_RULE_DIR))
    runner.pause(task_id)
    assert db.get_task(task_id)["status"] == "paused"
    runner.cancel(task_id)
    with pytest.raises(RuntimeError):
        runner.run(task_id)
    db.update_task(task_id, status="running")
    with pytest.raises(RuntimeError):
        runner.delete(task_id)
    db.update_task(task_id, status="completed")
    runner.delete(task_id)
    assert db.get_task(task_id) is None
    with pytest.raises(ValueError):
        runner.run(99999)
    with pytest.raises(FileNotFoundError):
        runner.create_task("坏目录", str(tmp_path / "missing"))


def test_rule_loading_failure_marks_task_failed(tmp_path, db, data_dir,
                                                sample_declare_text):
    root = make_archive(tmp_path, sample_declare_text, 1)
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task("坏规则", str(root), str(tmp_path / "no_rules"))
    with pytest.raises(Exception):
        runner.run(task_id)
    assert db.get_task(task_id)["status"] == "failed"
    assert db.get_task(task_id)["error_message"]


def test_concurrency_two(tmp_path, db, data_dir, sample_declare_text):
    root = make_archive(tmp_path, sample_declare_text, 6)
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task(
        "并发", str(root), str(DEFAULT_RULE_DIR), {"concurrency": 2}
    )
    task = runner.run(task_id)
    assert task["success_count"] == 6
    assert db.count_results({"task_id": task_id}) == 6
    assert json.loads(task["config_json"])["concurrency"] == 2
