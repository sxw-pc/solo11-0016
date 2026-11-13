"""Flask 本地管理服务：路由、筛选、快照与任务操作测试。"""
import time
from pathlib import Path

import pytest

from archive_extractor.config.loader import DEFAULT_RULE_DIR
from archive_extractor.service.app import create_app

MOCK = Path(__file__).resolve().parents[1] / "archive_extractor" / "mock_data"


@pytest.fixture
def app(db, data_dir, store, tracer, make_task, seed_result, tmp_path):
    task_id = make_task("服务测试任务")
    result_id = seed_result(task_id, {"declare_no": "SB-1", "applicant": "甲"})
    source = tmp_path / "source.html"
    source.write_text("<html><body>快照内容</body></html>", encoding="utf-8")
    relative = tracer.make_snapshot(str(source), "b" * 32, "source.html")
    db.execute(
        "UPDATE results SET snapshot_path = ?, form_type = 'public' WHERE id = ?",
        (relative, result_id),
    )
    db.add_validations(result_id, [{
        "level": "warning", "field": "applicant", "message": "需复核",
        "validator": "custom",
    }])
    application = create_app(
        db_path=data_dir / "archive.db", data_dir=data_dir / "files"
    )
    application.config["TESTING"] = True
    application.extensions["seed"] = (task_id, result_id)
    yield application
    application.extensions["db"].close()


def test_pages_and_filters(app):
    task_id, result_id = app.extensions["seed"]
    client = app.test_client()
    assert client.get("/").status_code == 302
    tasks = client.get("/tasks")
    assert tasks.status_code == 200 and "服务测试任务" in tasks.get_data(as_text=True)
    results = client.get(f"/results?task_id={task_id}&form_type=public&q=SB-1")
    assert results.status_code == 200
    assert "SB-1" in results.get_data(as_text=True)
    detail = client.get(f"/results/{result_id}")
    text = detail.get_data(as_text=True)
    assert detail.status_code == 200 and "需复核" in text and "快照内容" not in text
    snapshot = client.get(f"/snapshot/{result_id}")
    assert snapshot.status_code == 200 and "快照内容" in snapshot.get_data(as_text=True)
    snapshot.close()
    assert client.get("/results/9999").status_code == 404
    assert client.get("/snapshot/9999").status_code == 404
    assert client.get("/tasks?page=999").status_code == 200
    assert client.get("/nope").status_code == 404
    assert "https://" not in text and "cdn" not in text.lower()


def test_exports(app):
    task_id, result_id = app.extensions["seed"]
    client = app.test_client()
    for fmt in ("xlsx", "csv", "json"):
        response = client.get(f"/exports/{task_id}/{fmt}")
        assert response.status_code == 200
        assert int(response.headers["Content-Length"]) > 0
        response.close()
    errors = client.get(f"/exports/{task_id}/errors.csv")
    assert errors.status_code == 200 and "需复核" in errors.get_data(as_text=True)
    errors.close()
    assert client.get(f"/exports/{task_id}/pdf").status_code == 400
    assert client.get("/exports/9999/csv").status_code == 404


def test_task_creation_and_background_execution(app, tmp_path, db):
    root = tmp_path / "archive"
    root.mkdir()
    sample = (MOCK / "2023企业申报归档/normal/declare_002.html").read_text(
        encoding="utf-8"
    )
    (root / "form.html").write_text(sample, encoding="utf-8")
    client = app.test_client()
    response = client.post("/tasks", data={
        "name": "后台任务", "root_dir": str(root),
        "rules_dir": str(DEFAULT_RULE_DIR), "concurrency": "2", "timeout": "10",
    }, follow_redirects=True)
    assert response.status_code == 200 and "已创建" in response.get_data(as_text=True)
    service_db = app.extensions["db"]
    task = service_db.list_tasks(1)[0]
    client.post(f"/tasks/{task['id']}/start", follow_redirects=True)
    for _ in range(100):
        task = service_db.get_task(task["id"])
        if task["status"] not in {"running", "pending"}:
            break
        time.sleep(0.05)
    assert task["status"] == "completed"
    assert "后台任务" in client.get("/tasks").get_data(as_text=True)


def test_task_actions_and_restore(app, make_task, seed_result):
    _, result_id = app.extensions["seed"]
    task_id = make_task("操作任务")
    client = app.test_client()
    assert client.post(f"/tasks/{task_id}/pause").status_code == 302
    assert app.extensions["db"].get_task(task_id)["status"] == "paused"
    assert client.post(f"/tasks/{task_id}/retry").status_code == 302
    assert client.post(f"/tasks/{task_id}/cancel").status_code == 302
    assert client.post(f"/tasks/{task_id}/unknown").status_code == 404
    rerun = make_task("删除任务")
    assert client.post(f"/tasks/{rerun}/delete").status_code == 302
    assert app.extensions["db"].get_task(rerun) is None
    dup = seed_result(task_id, {"declare_no": "DUP"}, rel_path="dup.html")
    app.extensions["db"].execute(
        "UPDATE results SET is_deleted = 1, status = 'duplicate' WHERE id = ?",
        (dup,),
    )
    response = client.post(f"/results/{dup}/restore", follow_redirects=True)
    assert "已恢复" in response.get_data(as_text=True)
    assert client.post("/results/9999/restore").status_code == 302
    assert client.post("/tasks/9999/start").status_code == 404


def test_invalid_task_creation_and_remote_guard(app):
    client = app.test_client()
    response = client.post("/tasks", data={
        "name": "无效", "root_dir": "/path/does/not/exist",
    }, follow_redirects=True)
    assert "不存在" in response.get_data(as_text=True)
    response = client.post("/tasks", data={
        "name": "无效规则", "root_dir": str(MOCK),
        "rules_dir": "/path/does/not/exist",
    }, follow_redirects=True)
    assert "规则目录不存在" in response.get_data(as_text=True)
    remote = client.get("/tasks", environ_base={"REMOTE_ADDR": "203.0.113.5"})
    assert remote.status_code == 403
    assert "访问受限" in remote.get_data(as_text=True)
    response = client.post("/tasks", data={
        "name": "默认任务", "root_dir": str(MOCK),
    }, follow_redirects=True)
    assert "已创建" in response.get_data(as_text=True)


def test_start_twice_guard(app, tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    (root / "form.html").write_text(
        (MOCK / "2023企业申报归档/normal/declare_003.html").read_text(
            encoding="utf-8"
        ), encoding="utf-8"
    )
    client = app.test_client()
    client.post("/tasks", data={"name": "并发保护", "root_dir": str(root)})
    task = app.extensions["db"].list_tasks(1)[0]
    app.extensions["running"].add(task["id"])
    response = client.post(f"/tasks/{task['id']}/start", follow_redirects=True)
    assert "正在执行" in response.get_data(as_text=True)
    app.extensions["running"].discard(task["id"])
