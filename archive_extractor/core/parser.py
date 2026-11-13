"""基于 YAML 配置的 HTML 表单字段提取引擎。"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from lxml import html as lxml_html

from archive_extractor.utils.common import (
    ParseError,
    normalize_space,
    safe_float,
    utc_now,
)
MISSING = object()
TRUE_VALUES = {"1", "true", "yes", "y", "on", "是", "有", "checked"}
FALSE_VALUES = {"0", "false", "no", "n", "off", "否", "无", ""}
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日")


def _line_of(text: str, needle: str) -> int:
    """在原文中定位文本首次出现行号，找不到时返回 1。"""
    if not needle:
        return 1
    index = text.find(needle)
    if index < 0:
        return 1
    return text.count("\n", 0, index) + 1


def _node_text(node: Any, attr: Optional[str]) -> str:
    """从 BeautifulSoup 或 lxml 节点提取属性/文本。"""
    if attr:
        return str(node.get(attr, "") or "")
    if getattr(node, "name", "") == "select":
        options = node.select("option[selected]") or node.select("option")
        if options:
            option = options[0]
            return option.get("value") or option.get_text(" ", strip=True)
    if getattr(node, "name", "") == "textarea":
        return node.get_text()
    if hasattr(node, "get_text"):
        return node.get_text(" ", strip=True)
    return str(getattr(node, "text_content", lambda: "")()).strip()


def _convert(value: str, field_type: str, field: dict) -> Any:
    """按字段类型转换，转换失败抛 ValueError。"""
    if value is None:
        return None
    text = str(value)
    if field_type in {"text", "enum"}:
        return text
    if field_type == "number":
        number = safe_float(text)
        if number is None:
            raise ValueError("不是有效数字")
        return int(number) if number.is_integer() else number
    if field_type == "date":
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
        parsed = date_parser.parse(text, fuzzy=False)
        if isinstance(parsed, datetime):
            parsed = parsed.date()
        if not isinstance(parsed, date):
            raise ValueError("不是有效日期")
        return parsed.isoformat()
    if field_type == "boolean":
        lowered = text.strip().lower()
        if lowered in TRUE_VALUES:
            return True
        if lowered in FALSE_VALUES:
            return False
        raise ValueError("不是有效布尔值")
    return text


class FormExtractor:
    """匹配规则并抽取表单字段，同时保留字段级溯源信息。"""

    def __init__(self, ruleset) -> None:
        self.ruleset = ruleset

    def match_rule(self, soup: BeautifulSoup, raw_text: str) -> dict:
        """按表单选择器或标记匹配规则。"""
        for rule in self.ruleset.all():
            match = rule["match"]
            selector = match.get("form_selector")
            marker = match.get("marker", "")
            try:
                hit = bool(selector and soup.select_one(selector))
            except Exception:
                hit = False
            if hit or (marker and marker in raw_text):
                return rule
        raise ParseError("未匹配到任何已配置的表单规则")

    def extract(self, raw_text: str, source_hint: str = "") -> dict:
        """执行完整提取，返回数据、追踪项与非致命字段错误。"""
        if not raw_text.strip():
            raise ParseError("HTML 内容为空")
        try:
            soup = BeautifulSoup(raw_text, "lxml")
            tree = lxml_html.fromstring(raw_text)
        except Exception as exc:
            raise ParseError(f"HTML 解析失败: {exc}") from exc
        rule = self.match_rule(soup, raw_text)
        data: dict[str, Any] = {}
        traces: list[dict] = []
        errors: list[dict] = []
        for name, field in rule["fields"].items():
            try:
                value, field_traces = self._extract_field(
                    soup, tree, raw_text, name, field
                )
            except (ValueError, ParseError) as exc:
                value, field_traces = None, []
                errors.append({"field": name, "message": str(exc)})
            data[name] = value
            traces.extend(field_traces)
        return {
            "form_type": rule.get("form_type", rule["rule_id"]),
            "rule_id": rule["rule_id"],
            "rule_version": rule["_effective_version"],
            "rule_content_hash": rule.get("_content_hash", ""),
            "data": data,
            "traces": traces,
            "errors": errors,
            "extracted_at": utc_now(),
            "source": source_hint,
        }

    def _extract_field(
        self,
        soup: BeautifulSoup,
        tree: Any,
        raw_text: str,
        name: str,
        field: dict,
    ) -> tuple[Any, list[dict]]:
        if not field.get("selector") and "default" in field:
            return field["default"], []
        if field.get("type") == "object":
            return self._extract_object(soup, tree, raw_text, name, field)
        if field.get("selector_type", "css").lower() == "regex":
            return self._extract_regex(raw_text, name, field)
        nodes = self._select_nodes(soup, tree, field)
        multiple = field.get("type") == "array" or field.get("all", False)
        if not nodes:
            default = field.get("default", MISSING)
            return (None if default is MISSING else default), []
        selected = nodes if multiple else nodes[:1]
        values, traces = [], []
        for node, source_node in selected:
            raw = _node_text(node, field.get("attr"))
            prepared = self._preprocess(raw, field)
            original = raw
            if prepared == "" and "default" in field:
                value = field["default"]
            else:
                item_type = field.get("item_type", "text") if multiple else field.get(
                    "type", "text"
                )
                try:
                    value = _convert(prepared, item_type, field)
                except ValueError as exc:
                    raise ValueError(f"{field.get('label', name)}: {exc}") from exc
            line = getattr(source_node, "sourceline", None)
            line = int(line) if line else _line_of(raw_text, original or prepared)
            values.append(value)
            traces.append(self._trace(name, field, line, original, value))
        if multiple:
            return values, traces
        return values[0], traces

    def _extract_object(
        self,
        soup: BeautifulSoup,
        tree: Any,
        raw_text: str,
        name: str,
        field: dict,
    ) -> tuple[Any, list[dict]]:
        nodes = self._select_nodes(soup, tree, field)
        if not nodes:
            return None, []
        scope, _ = nodes[0]
        result, traces = {}, []
        for child_name, child in field.get("fields", {}).items():
            if child.get("type") == "object":
                raise ParseError(f"对象字段 {name} 不支持多层嵌套")
            child_nodes = scope.select(child.get("selector", ""))
            full_name = f"{name}.{child_name}"
            if not child_nodes:
                result[child_name] = child.get("default")
                continue
            raw = _node_text(child_nodes[0], child.get("attr"))
            prepared = self._preprocess(raw, child)
            try:
                value = _convert(prepared, child.get("type", "text"), child)
            except ValueError as exc:
                raise ValueError(f"{child.get('label', full_name)}: {exc}") from exc
            result[child_name] = value
            line = _line_of(raw_text, raw or prepared)
            traces.append(self._trace(full_name, child, line, raw, value))
        return result, traces

    def _extract_regex(
        self, raw_text: str, name: str, field: dict
    ) -> tuple[Any, list[dict]]:
        pattern = field.get("selector", "")
        try:
            regex = re.compile(pattern, re.MULTILINE | re.DOTALL)
        except re.error as exc:
            raise ParseError(f"正则表达式错误: {exc}") from exc
        matches = list(regex.finditer(raw_text))
        if not matches:
            default = field.get("default", MISSING)
            return (None if default is MISSING else default), []
        multiple = field.get("type") == "array" or field.get("all", False)
        chosen = matches if multiple else matches[:1]
        group = int(field.get("group", 0))
        values, traces = [], []
        for match in chosen:
            try:
                raw = match.group(group)
            except IndexError as exc:
                raise ParseError(f"正则分组 {group} 不存在") from exc
            prepared = self._preprocess(raw, field)
            item_type = field.get("item_type", "text") if multiple else field.get(
                "type", "text"
            )
            value = _convert(prepared, item_type, field)
            line = raw_text.count("\n", 0, match.start()) + 1
            values.append(value)
            traces.append(self._trace(name, field, line, raw, value))
        return (values if multiple else values[0]), traces

    @staticmethod
    def _select_nodes(
        soup: BeautifulSoup, tree: Any, field: dict
    ) -> list[tuple[Any, Any]]:
        selector = field.get("selector", "")
        selector_type = field.get("selector_type", "css").lower()
        if selector_type == "xpath":
            try:
                nodes = tree.xpath(selector)
            except Exception as exc:
                raise ParseError(f"XPath 无效: {exc}") from exc
            pairs = []
            for node in nodes:
                if not isinstance(node, str):
                    pairs.append((node, node))
            return pairs
        try:
            return [(node, None) for node in soup.select(selector)]
        except Exception as exc:
            raise ParseError(f"CSS 选择器无效: {exc}") from exc

    @staticmethod
    def _preprocess(raw: str, field: dict) -> str:
        value = "" if raw is None else str(raw)
        if field.get("trim", True):
            value = normalize_space(value)
        if field.get("remove_whitespace"):
            value = re.sub(r"\s+", "", value)
        for item in field.get("replacements", []):
            try:
                value = re.sub(item.get("pattern", ""), item.get("to", ""), value)
            except re.error as exc:
                raise ParseError(f"预处理正则错误: {exc}") from exc
        return value

    @staticmethod
    def _trace(name: str, field: dict, line: int, raw: str, value: Any) -> dict:
        return {
            "field": name,
            "selector": field.get("selector", ""),
            "selector_type": field.get("selector_type", "css"),
            "source_line": line,
            "raw_value": str(raw)[:500],
            "value": value,
            "extracted_at": utc_now(),
        }


def parse_html(raw_text: str, ruleset, source_hint: str = "") -> dict:
    """便捷函数：解析 HTML 文本。"""
    return FormExtractor(ruleset).extract(raw_text, source_hint)


def find_source_line(raw_text: str, needle: str) -> int:
    """公开的行号定位助手，供溯源模块复用。"""
    return _line_of(raw_text, needle)
