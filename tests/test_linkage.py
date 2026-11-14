"""字段联动层：依赖图、环检测、表达式求值与失败降级测试。"""
from __future__ import annotations

import pytest

from archive_extractor.core.linkage import LinkageEngine
from archive_extractor.core.linkage.expressions import ExpressionEvaluator, is_empty
from archive_extractor.core.linkage.graph import (
    DEGRADED,
    FAILED,
    OK,
    build_graph,
)
from archive_extractor.utils.common import RuleError


def _rule(derivations, constraints=None, fields=None):
    base = fields or {
        "a": {"type": "text", "default": "x"},
        "b": {"type": "text"},
        "c": {"type": "text"},
        "phone": {"type": "text"},
        "d1": {"type": "date"},
        "d2": {"type": "date"},
    }
    return {
        "rule_id": "linkage_form", "fields": base,
        "derivations": derivations, "constraints": constraints or [],
    }


# ---------------- 依赖图与环 ----------------

def test_topological_order_respects_dependencies():
    rule = _rule([
        {"field": "c", "expression": {"type": "slice", "field": "b", "start": 0}},
        {"field": "b", "expression": {"type": "slice", "field": "a", "start": 0}},
    ])
    graph = build_graph(rule)
    assert graph.order == ["b", "c"]
    assert graph.affected_by("a") == ["b", "c"]
    assert graph.affected_by("b") == ["c"]
    assert graph.affected_by("phone") == []


def test_cycle_rejected_at_load_time():
    rule = _rule([
        {"field": "a", "expression": {"type": "slice", "field": "b", "start": 0}},
        {"field": "b", "expression": {"type": "slice", "field": "c", "start": 0}},
        {"field": "c", "expression": {"type": "slice", "field": "a", "start": 0}},
    ])
    # a 同时有 default，先撞 selector 冲突；构造纯派生环单独验证
    rule["fields"] = {name: {"type": "text"} for name in ("a", "b", "c")}
    with pytest.raises(RuleError) as exc:
        build_graph(rule)
    assert "环" in str(exc.value)


def test_self_cycle_rejected():
    rule = _rule([
        {"field": "b", "expression": {"type": "slice", "field": "b", "start": 0}},
    ])
    with pytest.raises(RuleError, match="环"):
        build_graph(rule)


def test_unknown_field_reference_rejected():
    rule = _rule([
        {"field": "b", "expression": {"type": "slice", "field": "ghost", "start": 0}},
    ])
    with pytest.raises(RuleError, match="未声明的字段"):
        build_graph(rule)


def test_derived_field_cannot_have_selector():
    rule = _rule([
        {"field": "b", "expression": {"type": "slice", "field": "a", "start": 0}},
    ])
    rule["fields"]["b"] = {"type": "text", "selector": ".b"}
    with pytest.raises(RuleError, match="不能同时配置"):
        build_graph(rule)


def test_duplicate_derivation_rejected():
    expr = {"type": "slice", "field": "a", "start": 0}
    rule = _rule([
        {"field": "b", "expression": expr},
        {"field": "b", "expression": expr},
    ])
    with pytest.raises(RuleError, match="重复声明"):
        build_graph(rule)


def test_unknown_expression_type_rejected():
    rule = _rule([{"field": "b", "expression": {"type": "eval", "field": "a"}}])
    with pytest.raises(RuleError):
        build_graph(rule)


def test_case_requires_default_branch():
    rule = _rule([{
        "field": "b",
        "expression": {"type": "case", "field": "a",
                       "branches": [{"equals": "x", "value": "y"}]},
    }])
    with pytest.raises(RuleError, match="default"):
        build_graph(rule)


def test_constraint_reference_validation():
    rule = _rule([], [{
        "type": "date_order", "start": "d1", "end": "ghost",
    }])
    with pytest.raises(RuleError, match="未声明的字段"):
        build_graph(rule)


# ---------------- 表达式 ----------------

def test_concat_and_slice_expressions():
    ev = ExpressionEvaluator()
    data = {"code": "SB-2024-0077", "prefix": "310104"}
    assert ev.evaluate({"type": "slice", "field": "code", "start": 8}, data) == "0077"
    assert ev.evaluate({
        "type": "concat", "separator": "-",
        "parts": [{"field": "prefix"}, "字面", {"field": "code"}],
    }, data) == "310104-字面-SB-2024-0077"


def test_phone_kind_classification():
    ev = ExpressionEvaluator()
    assert ev.evaluate({"type": "phone_kind", "field": "p"}, {"p": "13800138000"}) == "手机"
    assert ev.evaluate({"type": "phone_kind", "field": "p"}, {"p": "010-66881234"}) == "固话"
    assert ev.evaluate({"type": "phone_kind", "field": "p"}, {"p": "12345"}) == "未知"


def test_case_and_coalesce_and_date_diff():
    ev = ExpressionEvaluator()
    case = {"type": "case", "field": "kind",
            "branches": [{"equals": "手机", "value": "短信"}], "default": "暂缓"}
    assert ev.evaluate(case, {"kind": "手机"}) == "短信"
    assert ev.evaluate(case, {"kind": "x"}) == "暂缓"
    assert ev.evaluate(
        {"type": "coalesce", "inputs": ["x", "y"]},
        {"x": None, "y": "兜底"},
    ) == "兜底"
    assert ev.evaluate(
        {"type": "date_diff", "start": "d1", "end": "d2"},
        {"d1": "2024-05-01", "d2": "2024-05-06"},
    ) == 5


def test_is_empty_semantics():
    assert is_empty(None) and is_empty("") and is_empty("  ") and is_empty([])
    assert not is_empty("手机") and not is_empty(0) and not is_empty(False)


# ---------------- 引擎求值与降级 ----------------

def _engine(rule):
    return LinkageEngine(rule, build_graph(rule))


def test_evaluate_chain_writes_values_and_traces():
    rule = _rule([
        {"field": "b", "expression": {"type": "slice", "field": "a", "start": 0, "end": 4}},
        {"field": "c", "expression": {
            "type": "concat", "parts": [{"field": "a"}, {"field": "b"}]}},
    ])
    engine = _engine(rule)
    extracted = {"data": {"a": "2024-REST"}, "errors": [], "rule_version": "v1"}
    out = engine.evaluate(extracted)
    assert out["data"]["b"] == "2024"
    assert out["data"]["c"] == "2024-REST2024"
    assert out["linkage"]["states"] == {"b": OK, "c": OK}
    trace = next(t for t in out["linkage"]["traces"] if t["field"] == "c")
    assert trace["derived"] == 1
    assert trace["upstream_fields"] == "a,b"
    assert trace["selector_type"] == "derivation"
    assert trace["linkage_status"] == OK
    assert trace["rule_version"] == "v1"


def test_upstream_failure_degrades_chain_without_dirty_value():
    rule = _rule([
        {"field": "b", "expression": {"type": "slice", "field": "a", "start": 0}},
        {"field": "c", "expression": {"type": "slice", "field": "b", "start": 0}},
    ])
    engine = _engine(rule)
    extracted = {
        "data": {"a": None}, "errors": [{"field": "a", "message": "转换失败"}],
        "rule_version": "v1",
    }
    out = engine.evaluate(extracted)
    assert out["data"]["b"] is None and out["data"]["c"] is None
    assert out["linkage"]["states"] == {"b": FAILED, "c": FAILED}
    fields = {i["field"] for i in out["linkage"]["issues"]}
    assert fields == {"b", "c"}
    trace = next(t for t in out["linkage"]["traces"] if t["field"] == "c")
    assert trace["linkage_status"] == FAILED and "降级" in trace["linkage_reason"]


def test_empty_upstream_degrades_to_none():
    rule = _rule([
        {"field": "b", "expression": {"type": "slice", "field": "a", "start": 0}},
    ])
    engine = _engine(rule)
    out = engine.evaluate({"data": {"a": ""}, "errors": [], "rule_version": "v"})
    assert out["data"]["b"] is None
    assert out["linkage"]["states"]["b"] == DEGRADED
    assert out["linkage"]["issues"][0]["level"] == "warning"


def test_unrelated_chain_still_computes_when_one_fails():
    rule = _rule([
        {"field": "b", "expression": {"type": "slice", "field": "a", "start": 0}},
        {"field": "c", "expression": {"type": "slice", "field": "phone", "start": 0}},
    ])
    engine = _engine(rule)
    out = engine.evaluate({
        "data": {"a": None, "phone": "13800138000"},
        "errors": [{"field": "a", "message": "bad"}], "rule_version": "v",
    })
    assert out["data"]["b"] is None
    assert out["data"]["c"] == "13800138000"


def test_rederive_only_recomputes_impacted_subgraph():
    rule = _rule([
        {"field": "b", "expression": {"type": "slice", "field": "a", "start": 0}},
        {"field": "c", "expression": {"type": "slice", "field": "b", "start": 0}},
    ])
    engine = _engine(rule)
    data = {"a": "2024-X", "b": "2024", "c": "2"}
    outcome = engine.rederive(data, ["a"], "v2", {"b": OK, "c": OK})
    assert outcome["recomputed"] == ["b", "c"]
    outcome_phone = engine.rederive(data, ["phone"], "v2", {"b": OK, "c": OK})
    assert outcome_phone["recomputed"] == []


def test_constraint_violation_and_skip():
    rule = _rule([], [
        {"type": "date_order", "start": "d1", "end": "d2", "level": "error",
         "message": "d2 早于 d1"},
    ])
    engine = _engine(rule)
    violated = engine.evaluate(
        {"data": {"d1": "2024-05-10", "d2": "2024-05-01"}, "errors": [],
         "rule_version": "v"}
    )
    assert violated["linkage"]["issues"][0]["validator"] == "linkage_constraint"
    statuses = [t["linkage_status"] for t in violated["linkage"]["traces"]
                if t["selector_type"] == "constraint"]
    assert statuses == ["violated"]
    skipped = engine.evaluate(
        {"data": {"d1": None, "d2": "2024-05-01"}, "errors": [], "rule_version": "v"}
    )
    assert not skipped["linkage"]["issues"]
    assert skipped["linkage"]["traces"][0]["linkage_status"] == "skipped"
