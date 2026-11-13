"""快照、字段溯源链与版本对比测试。"""
import json


def test_snapshot_is_immutable(tmp_path, store, tracer):
    source = tmp_path / "form.html"
    source.write_text("<html>原始内容</html>", encoding="utf-8")
    relative = tracer.make_snapshot(str(source), "a" * 32, "form.html")
    snapshot = store.resolve(relative)
    assert snapshot.exists()
    source.write_text("<html>修改后</html>", encoding="utf-8")
    assert "原始内容" in snapshot.read_text(encoding="utf-8")
    assert tracer.make_snapshot(str(source), "a" * 32, "form.html") == relative


def test_record_chain_and_snapshot_info(db, tracer, store, make_task, seed_result,
                                        tmp_path):
    task_id = make_task("溯源任务")
    result_id = seed_result(task_id, {"declare_no": "SB-1"})
    source = tmp_path / "source.html"
    source.write_text("<html>source</html>", encoding="utf-8")
    relative = tracer.make_snapshot(str(source), "a" * 32, "source.html")
    db.execute(
        "UPDATE results SET snapshot_path = ? WHERE id = ?", (relative, result_id)
    )
    db.add_validations(result_id, [{
        "level": "warning", "field": "x", "message": "提示",
        "validator": "test",
    }])
    tracer.record(result_id, [{
        "field": "declare_no", "selector": "input", "selector_type": "css",
        "source_line": 3, "raw_value": "SB-1", "extracted_at": "now",
    }], "a" * 32, "1.0.0+test")
    chain = tracer.chain(result_id)
    assert chain["result"]["id"] == result_id
    assert len(chain["traces"]) == 1
    assert len(chain["validations"]) == 1
    assert chain["history"][0]["version"] == 1
    info = tracer.snapshot_info(result_id)
    assert info["size"] > 0
    assert tracer.chain(9999) == {}
    assert tracer.snapshot_info(9999) == {}


def test_compare_versions(db, make_task, seed_result, tracer):
    task_id = make_task("版本任务")
    left = seed_result(task_id, {"value": 1})
    right = seed_result(task_id, {"value": 2})
    db.execute("UPDATE results SET version = 2 WHERE id = ?", (right,))
    diff = tracer.compare_versions(left, right)
    assert any("result-" in line for line in diff)
    assert any('+  "value": 2' in line for line in diff)
    assert tracer.compare_versions(left, 9999) == []


def test_chain_history_order(db, make_task, seed_result, tracer):
    task_id = make_task("历史任务")
    first = seed_result(task_id, {"v": 1})
    second = seed_result(task_id, {"v": 2})
    db.execute("UPDATE results SET version = 2 WHERE id = ?", (second,))
    chain = tracer.chain(first)
    assert [item["id"] for item in chain["history"]] == [first, second]
    assert json.loads(chain["result"]["data_json"]) == {"v": 1}
    assert chain["snapshot"] == ""
