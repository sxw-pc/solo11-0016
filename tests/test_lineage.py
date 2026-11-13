"""离线血缘聚合模块测试。

覆盖：规则断点、数据变化与规则变化的区分、无 trace 证据的显式降级、
跨批次字段缺失、产物确定性（重复运行逐字节一致）、顺序无关、
只读约束与清理。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from archive_extractor.lineage import LineageAggregator, lineage_run_id
from archive_extractor.lineage.aggregator import (
    ALIGN_DEGRADED,
    ALIGN_STABLE,
    DEGRADE_FIELD_MISSING,
    DEGRADE_NO_TRACE,
    LINEAGE_ARTIFACT_FILES,
    TRANSITION_DATA,
    TRANSITION_INDETERMINATE,
    TRANSITION_RULE,
    TRANSITION_RULE_AND_DATA,
)
from archive_extractor.storage.sqlite_client import SQLiteClient

MOCK = Path(__file__).resolve().parents[1] / "archive_extractor" / "mock_data"


def _rule_yaml(version: str, amount_selector: str, phone_selector: str,
               extra_field: bool = False) -> str:
    extra = (
        "  remark: {label: 备注, selector: 'input[name=\"remark\"]', "
        "attr: value, type: text, default: ''}\n"
        if extra_field
        else ""
    )
    return f"""rule_id: lineage_demo
name: 血缘演示表单
version: {version}
form_type: demo
match: {{form_selector: 'form#demoForm', marker: 'data-form-type="demo"'}}
fields:
  doc_no: {{label: 编号, selector: 'input[name="doc_no"]', attr: value,
           type: text, required: true}}
  amount: {{label: 金额, selector: '{amount_selector}', type: number,
           required: true}}
  contact_phone: {{label: 电话, selector: '{phone_selector}', attr: value,
                   type: text}}
  fixed: {{label: 固定值, type: text, default: 'K'}}
{extra}"""


def _html(doc_no: str, amount_a: str, amount_b: str, phone_main: str,
          phone_alt: str, remark: str = "") -> str:
    remark_input = (
        f'<input type="text" name="remark" value="{remark}">' if remark else ""
    )
    return f"""<!DOCTYPE html><html><body>
<form id="demoForm" data-form-type="demo">
  <input type="hidden" name="doc_no" value="{doc_no}">
  <div class="amount-a">{amount_a}</div>
  <div class="amount-b">{amount_b}</div>
  <input type="text" name="phone_main" value="{phone_main}">
  <input type="text" name="phone_alt" value="{phone_alt}">
  {remark_input}
</form></body></html>"""


@pytest.fixture
def lineage_env(tmp_path):
    """两批数据 × 两版规则的完整环境（走真实 TaskRunner 管线落库）。"""
    from archive_extractor.core.task_runner import TaskRunner

    db_path = tmp_path / "data" / "archive.db"
    db = SQLiteClient(db_path)
    data_dir = tmp_path / "data"
    rules_v1 = tmp_path / "rules_v1"
    rules_v2 = tmp_path / "rules_v2"
    rules_v1.mkdir()
    rules_v2.mkdir()
    # v1：amount 取 .amount-a，phone 取 phone_main，无 remark。
    (rules_v1 / "lineage_demo.yaml").write_text(
        _rule_yaml("1.0.0", "#demoForm .amount-a",
                   'input[name="phone_main"]'), encoding="utf-8"
    )
    # v2：amount 改取 .amount-b（规则+数据同时变），phone 改取 phone_alt
    # （仅选择器变、两输入框同值），新增 remark 字段（v1 缺失 -> 降级）。
    (rules_v2 / "lineage_demo.yaml").write_text(
        _rule_yaml("2.0.0", "#demoForm .amount-b",
                   'input[name="phone_alt"]', extra_field=True),
        encoding="utf-8",
    )
    batch1 = tmp_path / "batch1"
    batch2 = tmp_path / "batch2"
    batch1.mkdir()
    batch2.mkdir()
    # 同一来源路径、同一 doc_no：amount 两批不同（纯数据变化由 fixed/doc_no
    # 对照组覆盖），其余字段保持可对照。
    (batch1 / "form_1.html").write_text(
        _html("DOC-1", "100", "200", "13800000001", "13800000001"),
        encoding="utf-8",
    )
    (batch2 / "form_1.html").write_text(
        _html("DOC-1", "100", "200", "13800000001", "13800000001",
              "批次二备注"),
        encoding="utf-8",
    )
    runner = TaskRunner(db, data_dir / "files", timeout=10)
    task1 = runner.create_task(
        "批次一-规则v1", str(batch1), str(rules_v1), {"concurrency": 1}
    )
    runner.run(task1)
    task2 = runner.create_task(
        "批次二-规则v2", str(batch2), str(rules_v2), {"concurrency": 1}
    )
    runner.run(task2)
    output_dir = data_dir / "lineage"
    aggregator = LineageAggregator(db, output_dir)
    yield {
        "db": db,
        "aggregator": aggregator,
        "output_dir": output_dir,
        "data_dir": data_dir,
        "task_ids": [task1, task2],
    }
    db.close()


def _field(view: dict, field_path: str) -> dict:
    for item in view["fields"]:
        if item["field_path"] == field_path:
            return item
    raise AssertionError(f"字段未出现在血缘视图: {field_path}")


def _timeline(view: dict, field_path: str) -> list[dict]:
    return _field(view, field_path)["timeline"]


def _load_view(output_dir: Path, run_id: str) -> dict:
    return json.loads(
        (output_dir / run_id / "lineage_view.json").read_text(encoding="utf-8")
    )


def test_run_produces_three_artifacts_and_index(lineage_env):
    summary = lineage_env["aggregator"].run(lineage_env["task_ids"])
    run_dir = lineage_env["output_dir"] / summary["run_id"]
    for name in LINEAGE_ARTIFACT_FILES:
        assert (run_dir / name).is_file()
    index = json.loads(
        (lineage_env["output_dir"] / "runs_index.json").read_text(
            encoding="utf-8"
        )
    )
    assert index["runs"][0]["run_id"] == summary["run_id"]
    assert set(index["runs"][0]["artifacts"]) == set(LINEAGE_ARTIFACT_FILES)
    assert summary["run_id"] == lineage_run_id(
        list(reversed(lineage_env["task_ids"]))
    )


def test_rule_breakpoint_and_transition_classification(lineage_env):
    summary = lineage_env["aggregator"].run(lineage_env["task_ids"])
    view = _load_view(lineage_env["output_dir"], summary["run_id"])

    # amount：选择器 .amount-a -> .amount-b，且值 100 -> 200，规则+数据同时变。
    amount = _field(view, "amount")
    entry = amount["timeline"][1]
    assert entry["is_breakpoint"] is True
    assert entry["transition"] == TRANSITION_RULE_AND_DATA
    assert amount["timeline"][0]["transition"] is None
    assert amount["timeline"][0]["is_breakpoint"] is False

    # contact_phone：选择器变化但两个输入框同值 -> 纯规则断点。
    phone = _field(view, "contact_phone")
    assert phone["timeline"][1]["transition"] == TRANSITION_RULE
    assert phone["timeline"][1]["is_breakpoint"] is True

    # fixed：默认值字段无 trace，两批均缺证据 ->
    # 降级而非丢弃，且不误报断点。
    fixed = _field(view, "fixed")
    assert fixed["alignment_status"] == ALIGN_DEGRADED
    assert any(DEGRADE_NO_TRACE in reason for reason in fixed["degrade_reasons"])
    second = fixed["timeline"][1]
    assert second["transition"] == TRANSITION_INDETERMINATE
    assert second["degraded"] is True
    assert second["is_breakpoint"] is False

    # doc_no：选择器与值都不变 -> 无断点、无 transition。
    doc_no = _field(view, "doc_no")
    assert doc_no["alignment_status"] == ALIGN_STABLE
    assert all(item["is_breakpoint"] is False for item in doc_no["timeline"])
    assert doc_no["timeline"][1]["transition"] is None


def test_data_change_is_not_misclassified_as_rule_change(lineage_env):
    """选择器不变、仅值变化时必须标 data，不产生规则断点。"""
    db = lineage_env["db"]
    task2 = lineage_env["task_ids"][1]
    # 直接把批次二 doc_no 的提取值改掉，选择器保持原样。
    row = db.query_one(
        "SELECT id, data_json FROM results WHERE task_id = ? AND "
        "source_rel_path = ?",
        (task2, "form_1.html"),
    )
    data = json.loads(row["data_json"])
    data["doc_no"] = "DOC-CHANGED"
    db.execute(
        "UPDATE results SET data_json = ? WHERE id = ?",
        (json.dumps(data, ensure_ascii=False), row["id"]),
    )
    summary = lineage_env["aggregator"].run(lineage_env["task_ids"])
    view = _load_view(lineage_env["output_dir"], summary["run_id"])
    doc_no = _field(view, "doc_no")
    assert doc_no["timeline"][1]["transition"] == TRANSITION_DATA
    assert doc_no["timeline"][1]["is_breakpoint"] is False


def test_field_missing_in_earlier_batch_is_degraded(lineage_env):
    """remark 只存在于 v2 批次：对齐降级且首批显式缺失标记，不丢弃。"""
    summary = lineage_env["aggregator"].run(lineage_env["task_ids"])
    view = _load_view(lineage_env["output_dir"], summary["run_id"])
    remark = _field(view, "remark")
    assert remark["alignment_status"] == ALIGN_DEGRADED
    first, second = remark["timeline"]
    assert first["present"] is False
    assert first["degraded"] is True
    assert first["degrade_reason"] == DEGRADE_FIELD_MISSING
    assert second["present"] is True
    assert second["sources"][0]["value"] == "批次二备注"
    # 缺失批次不允许被伪装成规则断点。
    assert first["is_breakpoint"] is False


def test_timeline_carries_rule_version_selector_and_validation(lineage_env):
    summary = lineage_env["aggregator"].run(lineage_env["task_ids"])
    view = _load_view(lineage_env["output_dir"], summary["run_id"])
    amount = _field(view, "amount")
    first, second = amount["timeline"]
    assert first["rule_version"].startswith("1.0.0+")
    assert second["rule_version"].startswith("2.0.0+")
    assert first["selector"] == {
        "selector": "#demoForm .amount-a", "selector_type": "css"
    }
    assert second["selector"] == {
        "selector": "#demoForm .amount-b", "selector_type": "css"
    }
    source = second["sources"][0]
    assert source["value"] == 200
    assert source["raw_values"] == ["200"]
    assert source["validation_conclusion"] == "valid"
    assert source["result_status"] in {"valid", "warning"}


def test_repeated_runs_are_byte_identical(lineage_env):
    aggregator = lineage_env["aggregator"]
    first = aggregator.run(lineage_env["task_ids"])
    run_dir = lineage_env["output_dir"] / first["run_id"]
    snapshot = {
        name: (run_dir / name).read_bytes() for name in LINEAGE_ARTIFACT_FILES
    }
    index_bytes = (lineage_env["output_dir"] / "runs_index.json").read_bytes()
    second = aggregator.run(list(reversed(lineage_env["task_ids"])))
    assert second["run_id"] == first["run_id"]
    for name, content in snapshot.items():
        assert (run_dir / name).read_bytes() == content
    assert (lineage_env["output_dir"] / "runs_index.json").read_bytes() == \
        index_bytes
    # 同一批次集合只维护一个运行目录。
    assert [p.name for p in lineage_env["output_dir"].iterdir()
            if p.is_dir()] == [first["run_id"]]


def test_clean_removes_artifacts(lineage_env):
    aggregator = lineage_env["aggregator"]
    summary = aggregator.run(lineage_env["task_ids"])
    run_dir = lineage_env["output_dir"] / summary["run_id"]
    assert run_dir.exists()
    removed = aggregator.clean(summary["run_id"])
    assert not run_dir.exists()
    assert any(str(run_dir) in item for item in removed)
    index = json.loads(
        (lineage_env["output_dir"] / "runs_index.json").read_text(
            encoding="utf-8"
        )
    )
    assert index["runs"] == []
    # 全量清理后产物目录为空目录。
    aggregator.run(lineage_env["task_ids"])
    aggregator.clean()
    assert list(lineage_env["output_dir"].iterdir()) == []


def test_aggregation_is_read_only(lineage_env):
    """聚合前后：数据库逐表内容哈希不变，快照文件全部不变。"""
    db = lineage_env["db"]
    snapshot_files = sorted(
        p for p in (lineage_env["data_dir"] / "files").rglob("*")
        if p.is_file()
    )
    before_files = {
        str(p.relative_to(lineage_env["data_dir"])): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in snapshot_files
    }

    def db_fingerprint() -> str:
        parts = []
        for table in (
            "tasks", "task_files", "rules_versions", "results",
            "validations", "traces", "dedup_reports", "exports",
        ):
            rows = db.query(f"SELECT * FROM {table} ORDER BY id")
            parts.append(
                json.dumps(rows, ensure_ascii=False, sort_keys=True,
                           default=str)
            )
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    before_db = db_fingerprint()
    lineage_env["aggregator"].run(lineage_env["task_ids"])
    assert db_fingerprint() == before_db
    after_files = {
        str(p.relative_to(lineage_env["data_dir"])): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in snapshot_files
    }
    assert after_files == before_files
    # 聚合产物落在独立 lineage 目录，未写入 files/。
    assert not (lineage_env["data_dir"] / "files" / "lineage").exists()


def test_unknown_task_raises(lineage_env):
    with pytest.raises(ValueError, match="任务批次不存在"):
        lineage_env["aggregator"].run([99999])
    with pytest.raises(ValueError, match="至少需要指定一个任务批次"):
        lineage_env["aggregator"].run([])


def test_batch_manifest_records_rule_versions(lineage_env):
    summary = lineage_env["aggregator"].run(lineage_env["task_ids"])
    run_dir = lineage_env["output_dir"] / summary["run_id"]
    manifest = json.loads(
        (run_dir / "batch_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["ordering"] == lineage_env["task_ids"]
    versions = {item["version"] for item in manifest["rule_versions"]}
    assert any(v.startswith("1.0.0+") for v in versions)
    assert any(v.startswith("2.0.0+") for v in versions)
    assert manifest["manifest_hash"]
    alignment = json.loads(
        (run_dir / "field_alignment.json").read_text(encoding="utf-8")
    )
    assert alignment["alignment_hash"]
    keys = {item["canonical_key"] for item in alignment["fields"]}
    assert "demo.amount" in keys and "demo.remark" in keys


def test_validation_conclusions_and_invalid_result(tmp_path):
    """校验结论分级（valid/info/warning/invalid）写入时间轴来源明细。"""
    db = SQLiteClient(tmp_path / "archive.db")
    try:
        task_id = db.create_task("校验批次", str(tmp_path), "", {})
        result_id = db.store_result({
            "task_id": task_id, "form_type": "demo",
            "rule_id": "lineage_demo", "rule_version": "1.0.0+x",
            "rule_content_hash": "x",
            "data": {"amount": 1, "note": "n"}, "status": "invalid",
            "error_level": "error", "source_rel_path": "f.html",
            "source_md5": "b" * 32, "snapshot_path": "", "version": 1,
        })
        db.add_validations(result_id, [
            {"level": "error", "field": "amount", "message": "超限",
             "validator": "range"},
            {"level": "warning", "field": "note", "message": "建议复核",
             "validator": "required"},
        ])
        aggregator = LineageAggregator(db, tmp_path / "lineage")
        summary = aggregator.run([task_id])
        view = _load_view(tmp_path / "lineage", summary["run_id"])
        amount = _field(view, "amount")
        source = amount["timeline"][0]["sources"][0]
        assert source["validation_conclusion"] == "invalid"
        assert amount["timeline"][0]["validation_summary"][
            "highest_conclusion"
        ] == "invalid"
        note = _field(view, "note")
        assert note["timeline"][0]["sources"][0][
            "validation_conclusion"
        ] == "warning"
    finally:
        db.close()


def test_empty_batch_does_not_break_aggregation(tmp_path):
    """没有任何结果的批次仍出现在时间轴上，字段全部走缺失降级。"""
    db = SQLiteClient(tmp_path / "archive.db")
    try:
        empty_task = db.create_task("空批次", str(tmp_path), "", {})
        filled_task = db.create_task("有数据批次", str(tmp_path), "", {})
        db.store_result({
            "task_id": filled_task, "form_type": "demo",
            "rule_id": "lineage_demo", "rule_version": "1.0.0+x",
            "rule_content_hash": "x", "data": {"amount": 1},
            "status": "valid", "error_level": "",
            "source_rel_path": "f.html", "source_md5": "c" * 32,
            "snapshot_path": "", "version": 1,
        })
        aggregator = LineageAggregator(db, tmp_path / "lineage")
        summary = aggregator.run([empty_task, filled_task])
        view = _load_view(tmp_path / "lineage", summary["run_id"])
        assert view["batch_order"] == [empty_task, filled_task]
        amount = _field(view, "amount")
        assert amount["timeline"][0]["present"] is False
        assert amount["timeline"][0]["degrade_reason"] == \
            DEGRADE_FIELD_MISSING
        assert amount["timeline"][1]["present"] is True
    finally:
        db.close()


def test_run_id_is_order_independent():
    assert lineage_run_id([1, 2, 3]) == lineage_run_id([3, 1, 2])
    assert lineage_run_id([1, 2, 3]) != lineage_run_id([1, 2])
