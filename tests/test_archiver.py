"""台账导出、五 Sheet 结构与 SHA256 校验测试。"""
import csv
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from archive_extractor.core.archiver import Archiver


@pytest.fixture
def seeded(db, make_task, seed_result, tracer):
    task_id = make_task("导出任务")
    valid = seed_result(task_id, {"declare_no": "SB-1", "amount": 10},
                        rel_path="a.html", mtime=1)
    warning = seed_result(task_id, {"declare_no": "SB-2", "amount": 20},
                          rel_path="b.html", mtime=2, status="warning",
                          level="warning")
    invalid = seed_result(task_id, {"declare_no": "SB-3", "amount": None},
                          rel_path="c.html", mtime=3, status="invalid",
                          level="error")
    deleted = seed_result(task_id, {"declare_no": "SB-4"}, rel_path="d.html",
                          mtime=4)
    db.execute("UPDATE results SET is_deleted = 1, status = 'duplicate' "
               "WHERE id = ?", (deleted,))
    db.update_task(task_id, status="completed", total_files=3,
                   processed_files=3, success_count=2, failed_count=1)
    db.add_validations(valid, [{
        "level": "info", "field": "amount", "message": "提示",
        "validator": "compare"
    }])
    db.add_validations(invalid, [{
        "level": "error", "field": "amount", "message": "必填",
        "validator": "required"
    }])
    tracer.record(valid, [{
        "field": "declare_no", "selector": "input", "selector_type": "css",
        "source_line": 2, "raw_value": "SB-1",
    }], "a" * 32, "1.0.0+test")
    return task_id, valid, warning, invalid


def test_export_csv_json_excel(seeded, archiver, db):
    task_id, *_ = seeded
    csv_result = archiver.export(task_id, "csv")
    assert csv_result["records"] == 2
    assert Archiver.verify_export(csv_result["path"], csv_result["sha256"])
    with Path(csv_result["path"]).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert len(rows) == 3 and "申报编号" not in rows[0]
    json_result = archiver.export(task_id, "json")
    payload = json.loads(Path(json_result["path"]).read_text(encoding="utf-8"))
    assert payload["batch"]["total_results"] == 2
    assert len(payload["records"]) == 2
    assert len(payload["errors"]) == 2
    assert len(payload["traces"]) == 1
    xlsx_result = archiver.export(task_id, "xlsx")
    workbook = load_workbook(xlsx_result["path"])
    assert workbook.sheetnames == [
        "批次信息", "提取统计", "结果明细", "错误记录", "溯源索引"
    ]
    assert workbook["结果明细"].max_row == 3
    assert workbook["溯源索引"].max_row == 2
    workbook.close()
    assert db.query_one(
        "SELECT * FROM exports WHERE task_id = ? AND format = 'xlsx'", (task_id,)
    )


def test_export_options_and_filters(seeded, archiver, db):
    task_id, valid, *_ = seeded
    db.execute("UPDATE results SET form_type = 'public' WHERE id = ?", (valid,))
    result = archiver.export(
        task_id, "json", filters={"form_type": "public"},
        options={"include_trace": False, "include_errors": False,
                 "include_history": True},
    )
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert result["records"] == 1
    assert payload["errors"] == [] and payload["traces"] == []
    xlsx = archiver.export(
        task_id, "xlsx",
        options={"include_trace": False, "include_errors": False},
    )
    workbook = load_workbook(xlsx["path"])
    assert "未启用" in workbook["错误记录"]["E2"].value
    assert "未启用" in workbook["溯源索引"]["H2"].value
    workbook.close()


def test_error_list_and_tamper_detection(seeded, archiver, tmp_path):
    task_id, *_ = seeded
    output = tmp_path / "errors.csv"
    path = archiver.export_error_list(task_id, output)
    text = path.read_text(encoding="utf-8-sig")
    assert "必填" in text and "提示" in text
    result = archiver.export(task_id, "csv")
    Path(result["path"]).write_text("tampered", encoding="utf-8")
    assert Archiver.verify_export(result["path"], result["sha256"]) is False


def test_empty_and_invalid_format(seeded, archiver, db, make_task):
    task_id, *_ = seeded
    empty_id = make_task("空任务")
    result = archiver.export(empty_id, "json")
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert payload["records"] == []
    with pytest.raises(ValueError):
        archiver.export(task_id, "pdf")
