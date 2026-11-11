"""CLI 命令、演示与服务启动参数测试。"""
import json
import sqlite3
from pathlib import Path

from click.testing import CliRunner
from flask import Flask

from archive_extractor.cli import main

MOCK = Path(__file__).resolve().parents[1] / "archive_extractor" / "mock_data"


def invoke(runner, data_dir, *args):
    return runner.invoke(main, ["--data-dir", str(data_dir), *args])


def test_rules_init_scan_and_tasks(tmp_path):
    runner = CliRunner(mix_stderr=False)
    result = invoke(runner, tmp_path, "init")
    assert result.exit_code == 0 and "数据库" in result.output
    result = invoke(runner, tmp_path, "rules")
    assert "declare_form" in result.output and "签名" in result.output
    manifest = tmp_path / "manifest.json"
    result = invoke(runner, tmp_path, "scan", str(MOCK), "--output", str(manifest))
    assert result.exit_code == 0
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["total"] == 80
    result = invoke(runner, tmp_path, "tasks")
    assert result.exit_code == 0


def test_run_status_trace_export_and_actions(tmp_path):
    runner = CliRunner(mix_stderr=False)
    result = invoke(runner, tmp_path, "run", str(MOCK), "--name", "CLI",
                    "--concurrency", "2", "--limit", "4")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    task_id = payload["id"]
    assert payload["status"] == "paused"
    result = invoke(runner, tmp_path, "status", str(task_id))
    assert json.loads(result.output)["counts"]["processed"] == 4
    result = invoke(runner, tmp_path, "task-action", str(task_id), "resume")
    assert result.exit_code == 0 and "completed" in result.output
    result = invoke(runner, tmp_path, "tasks", "--limit", "5")
    assert "CLI" in result.output
    con = sqlite3.connect(tmp_path / "archive.db")
    try:
        result_id = con.execute(
            "SELECT id FROM results ORDER BY id LIMIT 1"
        ).fetchone()[0]
        task_id = con.execute("SELECT id FROM tasks").fetchone()[0]
    finally:
        con.close()
    result = invoke(runner, tmp_path, "trace", str(result_id))
    assert result.exit_code == 0 and "traces" in result.output
    result = invoke(runner, tmp_path, "export", str(task_id), "--format", "all")
    assert result.exit_code == 0
    assert result.output.count("SHA256=") == 3
    error_file = tmp_path / "errors.csv"
    result = invoke(runner, tmp_path, "errors", str(task_id), str(error_file))
    assert result.exit_code == 0 and error_file.exists()
    result = invoke(runner, tmp_path, "dedup", str(task_id))
    assert result.exit_code == 0 and "reason" in result.output


def test_missing_resource_errors(tmp_path):
    runner = CliRunner(mix_stderr=False)
    assert invoke(runner, tmp_path, "status", "999").exit_code != 0
    assert invoke(runner, tmp_path, "trace", "999").exit_code != 0
    assert invoke(runner, tmp_path, "export", "999").exit_code != 0


def test_demo_and_clean_demo(tmp_path):
    runner = CliRunner(mix_stderr=False)
    result = invoke(runner, tmp_path, "demo")
    assert result.exit_code == 0, result.output
    assert "档采匠" in result.output and "SHA256" in result.output
    assert (tmp_path / "demo.db").exists()
    result = invoke(runner, tmp_path, "clean-demo")
    assert result.exit_code == 0 and not (tmp_path / "demo.db").exists()


def test_service_uses_loopback_only(tmp_path, monkeypatch):
    captured = {}

    def fake_run(self, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(Flask, "run", fake_run)
    result = invoke(CliRunner(mix_stderr=False), tmp_path, "service", "--port", "5011")
    assert result.exit_code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 5011
    assert captured["debug"] is False
