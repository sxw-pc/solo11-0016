"""解析器边界分支补充测试。"""
import pytest
import yaml

from archive_extractor.config.loader import load_rules
from archive_extractor.core.parser import (
    FormExtractor,
    _line_of,
    _node_text,
    find_source_line,
)
from archive_extractor.utils.common import ParseError
from bs4 import BeautifulSoup


def build_rules(tmp_path, fields, match=None):
    rule = {
        "rule_id": "edge_form", "name": "边界", "version": "1.0.0",
        "match": match or {"form_selector": "#edge"}, "fields": fields,
    }
    (tmp_path / "edge.yaml").write_text(
        yaml.safe_dump(rule, allow_unicode=True), encoding="utf-8"
    )
    return load_rules(tmp_path)


def test_line_and_node_helpers():
    assert _line_of("a\nb", "b") == 2
    assert _line_of("a", "z") == 1
    assert _line_of("a", "") == 1
    assert find_source_line("x\ny", "y") == 2
    soup = BeautifulSoup("<select name=s></select>", "lxml")
    assert _node_text(soup.select_one("select"), None) == ""
    soup = BeautifulSoup("<textarea> abc </textarea>", "lxml")
    assert "abc" in _node_text(soup.select_one("textarea"), None)
    with pytest.raises(ParseError):
        FormExtractor._select_nodes(
            None, None, {"selector": "//*[", "selector_type": "xpath"}
        )
    with pytest.raises(ParseError):
        FormExtractor._select_nodes(
            soup, None, {"selector": "[", "selector_type": "css"}
        )


def test_select_object_defaults_and_regex(tmp_path):
    rules = build_rules(tmp_path, {
        "choice": {"selector": "select[name=s]"},
        "missing_obj": {"type": "object", "selector": ".none",
                        "fields": {"x": {"selector": ".x"}}},
        "obj_with_default": {
            "type": "object", "selector": ".box",
            "fields": {
                "a": {"selector": ".missing", "default": "fallback"},
                "b": {"selector": ".number", "type": "number"},
            },
        },
        "code": {"selector": r"编号：(\w+)", "selector_type": "regex",
                 "default": "NONE"},
        "blank": {"selector": ".does-not-exist", "default": "D"},
    })
    html = """<form id="edge">
      <select name="s"><option value="A">甲</option><option value="B">乙</option></select>
      <div class="box"><span class="number">12</span></div>
      <span>无编号</span>
    </form>"""
    data = FormExtractor(rules).extract(html)["data"]
    assert data["choice"] == "A"
    assert data["missing_obj"] is None
    assert data["obj_with_default"] == {"a": "fallback", "b": 12}
    assert data["code"] == "NONE"
    assert data["blank"] == "D"


def test_date_fallback_boolean_error_and_bad_selectors(tmp_path):
    rules = build_rules(tmp_path, {
        "when": {"selector": ".date", "type": "date"},
        "flag": {"selector": ".flag", "type": "boolean"},
        "weird": {"selector": ".weird", "type": "date"},
    })
    html = """<form id="edge"><span class="date">Jan 02, 2023</span>
      <span class="flag">maybe</span><span class="weird">not-a-date</span></form>"""
    result = FormExtractor(rules).extract(html)
    assert result["data"]["when"] == "2023-01-02"
    assert {item["field"] for item in result["errors"]} == {"flag", "weird"}
    bad_css = build_rules(tmp_path, {
        "x": {"selector": "[", "selector_type": "css"}
    })
    assert FormExtractor(bad_css).extract(
        '<form id="edge">x</form>'
    )["errors"][0]["field"] == "x"
    bad_xpath = build_rules(tmp_path, {
        "x": {"selector": "//*[", "selector_type": "xpath"}
    })
    assert FormExtractor(bad_xpath).extract(
        '<form id="edge">x</form>'
    )["errors"][0]["field"] == "x"


def test_regex_compile_error(tmp_path):
    rules = build_rules(tmp_path, {
        "code": {"selector": "(", "selector_type": "regex"}
    })
    result = FormExtractor(rules).extract('<form id="edge">x</form>')
    assert result["errors"][0]["field"] == "code"


def test_object_conversion_error(tmp_path):
    rules = build_rules(tmp_path, {
        "box": {
            "type": "object", "selector": ".box",
            "fields": {"n": {"selector": ".n", "type": "number"}},
        }
    })
    with pytest.raises(ValueError):
        FormExtractor(rules)._extract_object(
            BeautifulSoup('<form id="edge"><div class="box">'
                          '<span class="n">bad</span></div></form>', "lxml"),
            None, "x", "box", rules.all()[0]["fields"]["box"],
        )
