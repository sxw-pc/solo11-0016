"""仲裁 CLI 与 Flask 路由集成测试。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from archive_extractor.cli import main
from archive_extractor.config.loader import DEFAULT_RULE_DIR
from archive_extractor.core.task_runner import TaskRunner
from archive_extractor.service.app import create_app

MOCK = Path(__file__).resolve().parents[1] / "archive_extractor" / "mock_data"


def _form(declare_no, scope, amount):
    return f"""<!DOCTYPE html><html><body>
<form id="declareForm" data-form-type="declare">
  <input type="hidden" name="declare_no" value="{declare_no}">
  <input type="hidden" name="credit_code" value="91110000MA00000001">
  <div class="applicant">测试企业</div>
  <select name="category"><option value="税收优惠" selected></option></select>
  <input type="text" name="contact_name" value="联系人">
  <input type="text" name="contact_phone" value="13800000001">
  <input type="date" name="apply_date" value="2023-05-01">
  <div class="project-name">测试项目</div>
  <div class="amount">{amount}</div>
  <input type="text" name="is_urgent" value="否">
  <textarea name="scope">{scope}</textarea>
  <div class="contact-box"><span class="dept">综合部</span>
    <span class="email" data-email="a@example.test"></span></div>
</form></body></html>"""


def _make_conflict_dir(tmp_path):
    root = tmp_path / "conflict"
    root.mkdir()
    old = root / "old.html"
    new = root / "new.html"
    old.write_text(_form("SB-2023-8001", "旧范围", "1000"), encoding="utf-8")
    new.write_text(_form("SB-2023-8001", "", "2000"), encoding="utf-8")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    return root


def _run_task(db, data_dir, root):
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task("仲裁集成", str(root), str(DEFAULT_RULE_DIR))
    runner.run(task_id)
    return task_id


def invoke(runner, data_dir, *args):
    return runner.invoke(main, ["--data-dir", str(data_dir), *args])


def test_cli_arbitration_lifecycle(tmp_path, db, data_dir):
    root = _make_conflict_dir(tmp_path)
    task_id = _run_task(db, data_dir, root)
    runner = CliRunner(mix_stderr=False)
    # 列表
    result = invoke(runner, data_dir, "arbitration-list", "--task-id", str(task_id))
    assert result.exit_code == 0
    batch_no = json.loads(result.output)[0]["batch_no"]
    # 详情含字段档案
    result = invoke(runner, data_dir, "arbitration-detail", batch_no)
    assert result.exit_code == 0
    detail = json.loads(result.output)
    assert detail["field_records"] and detail["batch"]["status"] == "committed"
    # 回放一致
    result = invoke(runner, data_dir, "arbitration-replay", batch_no)
    assert result.exit_code == 0
    assert json.loads(result.output)["consistent"] is True
    # 三种格式独立导出
    for fmt in ("csv", "json", "xlsx"):
        result = invoke(runner, data_dir, "arbitration-export", str(task_id),
                        "--format", fmt)
        assert result.exit_code == 0 and "仲裁档案" in result.output
    # 撤销
    result = invoke(runner, data_dir, "arbitration-rollback", batch_no)
    assert result.exit_code == 0
    assert json.loads(result.output)["status"] == "rolled_back"
    assert db.query("SELECT COUNT(*) n FROM results")[0]["n"] == 0
    # 重复撤销报错而非部分成功
    result = invoke(runner, data_dir, "arbitration-rollback", batch_no)
    assert result.exit_code != 0 and "已撤销" in result.stderr
    # 找不到批次
    result = invoke(runner, data_dir, "arbitration-detail", "ARB-NOPE")
    assert result.exit_code != 0 and "找不到仲裁记录" in result.stderr


def test_cli_arbitration_export_missing_record(tmp_path, db, data_dir):
    task_id = db.create_task("空任务", str(tmp_path), "", {})
    runner = CliRunner(mix_stderr=False)
    result = invoke(runner, data_dir, "arbitration-export", str(task_id),
                    "--format", "json")
    assert result.exit_code != 0 and "找不到仲裁记录" in result.stderr


def test_web_arbitration_pages_and_rollback(tmp_path, db, data_dir):
    root = _make_conflict_dir(tmp_path)
    task_id = _run_task(db, data_dir, root)
    batch_no = db.list_arbitration_batches(task_id)[0]["batch_no"]
    app = create_app(db_path=data_dir / "archive.db",
                     data_dir=data_dir / "files")
    app.config["TESTING"] = True
    client = app.test_client()
    try:
        page = client.get("/arbitration")
        assert page.status_code == 200 and batch_no in page.get_data(as_text=True)
        detail = client.get(f"/arbitration/{batch_no}")
        assert detail.status_code == 200
        assert "字段仲裁档案" in detail.get_data(as_text=True)
        # 独立档案导出
        for fmt in ("csv", "json", "xlsx"):
            resp = client.get(f"/arbitration/{task_id}/{fmt}")
            assert resp.status_code == 200 and int(
                resp.headers["Content-Length"]
            ) > 0
            resp.close()
        # 结果溯源页出现仲裁采用卡片
        winner = db.query_one(
            "SELECT id FROM results WHERE is_deleted = 0"
        )
        trace_page = client.get(f"/results/{winner['id']}")
        assert "字段仲裁采用" in trace_page.get_data(as_text=True)
        # 整批撤销
        resp = client.post(f"/arbitration/{batch_no}/rollback",
                           follow_redirects=True)
        assert resp.status_code == 200 and "已撤销" in resp.get_data(as_text=True)
        assert db.get_arbitration_batch_by_no(batch_no)["status"] == "rolled_back"
        # 撤销后再访问详情仍可看档案（保留审计痕迹）
        assert client.get(f"/arbitration/{batch_no}").status_code == 200
        # 不存在的批次 404
        assert client.get("/arbitration/ARB-NOPE").status_code == 404
    finally:
        app.extensions["db"].close()


def test_web_rollback_unknown_batch_flashes_error(tmp_path, db, data_dir):
    app = create_app(db_path=data_dir / "archive.db",
                     data_dir=data_dir / "files")
    app.config["TESTING"] = True
    client = app.test_client()
    try:
        resp = client.post("/arbitration/ARB-NOPE/rollback",
                           follow_redirects=True)
        assert resp.status_code == 200
        assert "找不到仲裁记录" in resp.get_data(as_text=True)
    finally:
        app.extensions["db"].close()
