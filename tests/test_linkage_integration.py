"""字段联动端到端集成：规则加载、任务执行、局部重算与导出溯源。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from archive_extractor.config.loader import load_rules
from archive_extractor.core.task_runner import TaskRunner
from archive_extractor.utils.common import RuleError

MOCK = Path(__file__).resolve().parents[1] / "archive_extractor" / "mock_data"


def _write_rule(tmp_path, payload: dict) -> Path:
    rule_dir = tmp_path / "rules"
    rule_dir.mkdir(exist_ok=True)
    path = rule_dir / "link.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return rule_dir


def _base_rule(derivations, constraints=None):
    return {
        "rule_id": "link_form", "name": "联动表单", "version": "1.0.0",
        "match": {"form_selector": "form#linkForm"},
        "fields": {
            "region": {"selector": 'input[name="region"]', "attr": "value",
                       "type": "text", "default": "110101"},
            "doc_no": {"selector": 'input[name="doc_no"]', "attr": "value",
                       "type": "text", "required": True},
            "apply_date": {"selector": 'input[name="doc_date"]', "attr": "value",
                           "type": "date", "required": True},
            "phone": {"selector": 'input[name="phone"]', "attr": "value",
                      "type": "text", "required": True},
            "record_no": {"type": "text"},
            "year": {"type": "text"},
            "phone_type": {"type": "text"},
            "notify": {"type": "enum"},
        },
        "derivations": derivations,
        "constraints": constraints or [],
    }


def test_cyclic_rule_rejected_when_loading(tmp_path):
    payload = _base_rule([
        {"field": "year", "expression": {"type": "slice", "field": "record_no",
                                         "start": 0}},
        {"field": "record_no", "expression": {
            "type": "concat", "parts": [{"field": "year"}]}},
    ])
    # 参与环的派生字段用纯字段声明；未参与联动的字段仍需 default 才能过 schema
    for name in ("year", "record_no"):
        payload["fields"][name] = {"type": "text"}
    payload["fields"]["phone_type"] = {"type": "text", "default": ""}
    payload["fields"]["notify"] = {"type": "enum", "default": ""}
    rule_dir = _write_rule(tmp_path, payload)
    with pytest.raises(RuleError, match="环"):
        load_rules(rule_dir)


def test_end_to_end_derivation_and_trace_persistence(
        tmp_path, db, data_dir, sample_declare_text):
    runner = TaskRunner(db, data_dir / "files")
    ruleset = load_rules()
    root = tmp_path / "archive"
    root.mkdir()
    (root / "form.html").write_text(sample_declare_text, encoding="utf-8")
    task_id = runner.create_task(
        "联动", str(root), str(ruleset.source_dir), {"concurrency": 1}
    )
    task = runner.run(task_id)
    assert task["success_count"] == 1
    result = db.list_results({"task_id": task_id})[0]
    data = json.loads(result["data_json"])
    # 与手工核算一致
    assert data["record_no"] == "110101-2023-0001"
    assert data["phone_type"] == "手机"
    assert data["notify_status"] == "短信通知"
    assert data["apply_year"] == "2023" and data["serial_no"] == "0001"
    # 溯源索引含派生链路
    trace = db.query_one(
        """SELECT * FROM traces WHERE result_id = ? AND field = 'record_no'""",
        (result["id"],),
    )
    assert trace["selector_type"] == "derivation"
    assert trace["derived"] == 1
    assert trace["upstream_fields"] == "district_code,apply_year,serial_no"
    assert trace["expression"].startswith("concat")
    assert trace["linkage_status"] == "ok"
    assert trace["rule_version"].startswith("1.1.0+")


def test_failure_degradation_recorded_in_result_and_trace(
        tmp_path, db, data_dir):
    # apply_date 非法 -> apply_year/record_no 级联降级，结果 invalid 且可查
    html = (
        '<form id="declareForm" data-form-type="declare">'
        '<input name="declare_no" value="SB-2023-0999">'
        '<input name="credit_code" value="91110000MA00000999">'
        '<span class="applicant">示例申报企业0999</span>'
        '<select name="category"><option selected>税收优惠</option></select>'
        '<input name="contact_name" value="联系人0999">'
        '<input name="contact_phone" value="13800000999">'
        '<input name="apply_date" value="2023-13-45">'
        '<div class="project-name">项目0999</div>'
        '<div class="amount">100</div></form>'
    )
    runner = TaskRunner(db, data_dir / "files")
    ruleset = load_rules()
    root = tmp_path / "archive"
    root.mkdir()
    (root / "bad.html").write_text(html, encoding="utf-8")
    task_id = runner.create_task(
        "降级", str(root), str(ruleset.source_dir), {"concurrency": 1}
    )
    runner.run(task_id)
    result = db.list_results({"task_id": task_id})[0]
    assert result["status"] == "invalid"
    data = json.loads(result["data_json"])
    assert data["apply_year"] is None and data["record_no"] is None
    validations = db.query(
        "SELECT field, validator FROM validations WHERE result_id = ?",
        (result["id"],),
    )
    assert any(v["validator"] == "linkage_derivation" for v in validations)
    degraded = db.query(
        """SELECT field, linkage_status, linkage_reason FROM traces
           WHERE result_id = ? AND selector_type = 'derivation'
           AND linkage_status != 'ok'""",
        (result["id"],),
    )
    assert {row["field"] for row in degraded} >= {"apply_year", "record_no"}
    assert all("降级" in row["linkage_reason"] for row in degraded)


def test_rederive_impact_scope_and_versioning(
        tmp_path, db, data_dir, sample_declare_text):
    runner = TaskRunner(db, data_dir / "files")
    ruleset = load_rules()
    root = tmp_path / "archive"
    root.mkdir()
    rel = "form.html"
    (root / rel).write_text(sample_declare_text, encoding="utf-8")
    task_id = runner.create_task(
        "重算", str(root), str(ruleset.source_dir), {"concurrency": 1}
    )
    runner.run(task_id)
    first = db.list_results({"task_id": task_id})[0]
    data = json.loads(first["data_json"])
    data["contact_phone"] = "13911112222"
    db.execute(
        "UPDATE results SET data_json = ? WHERE id = ?",
        (json.dumps(data, ensure_ascii=False), first["id"]),
    )
    outcome = runner.rederive(task_id, rel, ["contact_phone"])
    assert outcome["recomputed"] == ["phone_type", "notify_status"]
    assert outcome["changed"] is True
    latest = db.list_results({"task_id": task_id})[0]
    assert latest["id"] != first["id"] and latest["status"] == "valid"
    # 旧版本被置为 superseded，溯源仍保留
    old = db.get_result(first["id"])
    assert old["status"] == "superseded" and old["is_deleted"] == 1
    new_traces = db.query(
        """SELECT field FROM traces WHERE result_id = ?
           AND selector_type = 'derivation'""",
        (latest["id"],),
    )
    assert {row["field"] for row in new_traces} == {"phone_type", "notify_status"}


def test_rederive_unknown_field_rejected(tmp_path, db, data_dir,
                                         sample_declare_text):
    runner = TaskRunner(db, data_dir / "files")
    ruleset = load_rules()
    root = tmp_path / "archive"
    root.mkdir()
    (root / "form.html").write_text(sample_declare_text, encoding="utf-8")
    task_id = runner.create_task(
        "重算", str(root), str(ruleset.source_dir), {"concurrency": 1}
    )
    runner.run(task_id)
    with pytest.raises(ValueError, match="不属于该规则"):
        runner.rederive(task_id, "form.html", ["ghost_field"])


def test_export_contains_linkage_rows(tmp_path, db, data_dir,
                                      sample_declare_text, archiver):
    runner = TaskRunner(db, data_dir / "files")
    ruleset = load_rules()
    root = tmp_path / "archive"
    root.mkdir()
    (root / "form.html").write_text(sample_declare_text, encoding="utf-8")
    task_id = runner.create_task(
        "导出联动", str(root), str(ruleset.source_dir), {"concurrency": 1}
    )
    runner.run(task_id)
    result = archiver.export(task_id, "json")
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    kinds = {row["selector_type"] for row in payload["traces"]}
    assert "derivation" in kinds and "constraint" in kinds
    derived = next(row for row in payload["traces"]
                   if row["selector_type"] == "derivation")
    assert derived["upstream_fields"] and derived["linkage_status"] == "ok"
