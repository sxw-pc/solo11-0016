"""HTML 字段提取、类型转换与溯源行号测试。"""
import pytest
import yaml

from archive_extractor.config.loader import load_rules
from archive_extractor.core.parser import FormExtractor, parse_html
from archive_extractor.utils.common import ParseError

CUSTOM_RULE = {
    "rule_id": "custom_form",
    "name": "自定义表单",
    "version": "1.0.0",
    "match": {"form_selector": "#customForm", "marker": "custom-marker"},
    "fields": {
        "title": {"selector": '//div[@id="title"]', "selector_type": "xpath"},
        "code": {
            "selector": r"编号[:：]\s*([A-Z0-9-]+)",
            "selector_type": "regex",
            "group": 1,
        },
        "amount": {"selector": ".amount", "type": "number"},
        "active": {"selector": "#active", "attr": "value", "type": "boolean"},
        "tags": {
            "selector": "input[name=tags]:checked",
            "attr": "value",
            "type": "array",
            "all": True,
        },
        "fallback": {"default": "默认值"},
        "clean": {
            "selector": ".clean",
            "replacements": [{"pattern": r"\s+", "to": ""}],
        },
    },
}


@pytest.fixture
def custom_rules(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text(yaml.safe_dump(CUSTOM_RULE, allow_unicode=True), encoding="utf-8")
    return load_rules(tmp_path)


def test_extract_declare_full(sample_declare_text, ruleset):
    result = parse_html(sample_declare_text, ruleset)
    data = result["data"]
    assert result["form_type"] == "declare"
    assert data["declare_no"] == "SB-2023-0001"
    assert data["category"] == "税收优惠"
    assert data["amount"] == 481557.99
    assert data["is_urgent"] is True
    assert data["invoice_needed"] is True
    assert data["attachments"] == ["scan_0001.pdf", "list_0001.txt"]
    assert data["contact"] == {
        "department": "综合部", "email": "dept0001@example.test"
    }
    fields = {trace["field"] for trace in result["traces"]}
    assert {"declare_no", "contact.department", "attachments"} <= fields
    assert all(trace["source_line"] >= 1 for trace in result["traces"])
    assert result["rule_version"].startswith("1.0.0+")
    assert not result["errors"]


def test_extract_public_table_and_register(sample_public_text,
                                           sample_register_text, ruleset):
    public = parse_html(sample_public_text, ruleset)["data"]
    assert public["title"].startswith("关于示例事项")
    assert public["period_end"] >= public["period_start"]
    assert public["categories"] == ["档案", "公开"]
    assert public["issuer"]["office"] == "档案管理处"
    register = parse_html(sample_register_text, ruleset)["data"]
    assert register["reg_no"] == "DJ-2024-0001"
    assert register["registered_capital"] == 100000
    assert register["tags"] == ["重点", "归档"]


def test_custom_rule_css_xpath_regex(custom_rules):
    html = """<html><body>
    <form id="customForm" data-x="custom-marker">
      <div id="title">测试标题</div>
      <span>编号：ABC-100</span>
      <span class="amount">1,200.50</span>
      <input id="active" value="是">
      <input type="checkbox" name="tags" value="a" checked>
      <input type="checkbox" name="tags" value="b" checked>
      <span class="clean">空 格 文 本</span>
    </form></body></html>"""
    result = FormExtractor(custom_rules).extract(html)
    data = result["data"]
    assert data["title"] == "测试标题"
    assert data["code"] == "ABC-100"
    assert data["amount"] == 1200.5
    assert data["active"] is True
    assert data["tags"] == ["a", "b"]
    assert data["fallback"] == "默认值"
    assert data["clean"] == "空格文本"


def test_regex_all_and_group_error(tmp_path, custom_rules):
    html = '<form id="customForm">编号：A-1 编号：B-2 <div id="title">T</div></form>'
    rule = CUSTOM_RULE.copy()
    rule["fields"] = {
        "codes": {
            "selector": r"编号[:：]\s*([A-Z]-[0-9])",
            "selector_type": "regex",
            "group": 1,
            "type": "array",
            "all": True,
        }
    }
    path = tmp_path / "custom.yaml"
    path.write_text(yaml.safe_dump(rule, allow_unicode=True), encoding="utf-8")
    rules = load_rules(tmp_path)
    assert parse_html(html, rules)["data"]["codes"] == ["A-1", "B-2"]
    with pytest.raises(ParseError):
        FormExtractor(rules)._extract_regex("a", "x", {
            "selector": "(a)", "group": 3, "type": "text"
        })


def test_conversion_errors_and_missing_form(ruleset):
    html = (
        '<form id="declareForm" data-form-type="declare">'
        '<input type="hidden" name="declare_no" value="BAD">'
        '<input type="date" name="apply_date" value="2023-13-45">'
        "</form>"
    )
    result = parse_html(html, ruleset)
    fields = {item["field"] for item in result["errors"]}
    assert {"apply_date"} <= fields
    assert result["data"]["declare_no"] == "BAD"
    with pytest.raises(ParseError):
        parse_html("<html>普通新闻页面</html>", ruleset)
    with pytest.raises(ParseError):
        parse_html("   ", ruleset)


def test_marker_fallback(sample_declare_text, ruleset):
    broken = sample_declare_text.replace("<form", "<div").replace("</form>", "</div>")
    result = parse_html(broken, ruleset)
    assert result["rule_id"] == "declare_form"


def test_object_selector_invalid(tmp_path, ruleset):
    extractor = FormExtractor(ruleset)
    with pytest.raises(ParseError):
        extractor._select_nodes(None, None, {"selector": "[", "selector_type": "css"})
