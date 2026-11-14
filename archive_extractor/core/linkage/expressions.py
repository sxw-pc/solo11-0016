"""联动表达式白名单求值器。

不使用 ``eval``/``exec``，每类表达式都是显式实现的纯函数，
只读取引擎解析好的字段值，无法访问环境、模块或文件系统。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from dateutil import parser as date_parser

from archive_extractor.core.linkage.graph import LinkageError

MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")
LANDLINE_RE = re.compile(r"^(0\d{2,3})?[-\s]?\d{7,8}(?:-\d{1,6})?$")


def is_empty(value: Any) -> bool:
    """联动语义下的空值：None、纯空白字符串、空容器。

    注意 False、0 以及任意非空文本（包括中文枚举值）都不算空，
    否则条件映射的结果会被误判为缺失并错误降级。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict, tuple, set)):
        return not value
    return False


class ExpressionEvaluator:
    """按表达式类型分派的无状态求值器。"""

    def evaluate(self, expression: dict, values: dict[str, Any]) -> Any:
        kind = expression.get("type")
        handler = getattr(self, f"_expr_{kind}", None)
        if handler is None:
            raise LinkageError(f"不支持的表达式类型: {kind}")
        return handler(expression, values)

    # ---- concat：字面量与字段值按顺序拼接 ----
    def _expr_concat(self, expr: dict, values: dict) -> str:
        parts = expr.get("parts")
        if not isinstance(parts, list) or not parts:
            raise LinkageError("concat 表达式需要非空 parts 列表")
        separator = str(expr.get("separator", ""))
        rendered: list[str] = []
        for part in parts:
            if isinstance(part, str):
                rendered.append(part)
            elif isinstance(part, dict) and "field" in part:
                rendered.append(self._to_text(values[part["field"]]))
            else:
                raise LinkageError(f"concat 片段非法: {part!r}")
        return separator.join(rendered)

    # ---- slice：字符串切片，用于取年份等固定片段 ----
    def _expr_slice(self, expr: dict, values: dict) -> str:
        field = expr.get("field")
        text = self._to_text(values.get(field))
        start = expr.get("start", 0)
        end = expr.get("end")
        if not isinstance(start, int) or (end is not None and not isinstance(
            end, int
        )):
            raise LinkageError("slice 的 start/end 必须是整数")
        return text[start:end] if end is not None else text[start:]

    # ---- coalesce：返回第一个非空输入 ----
    def _expr_coalesce(self, expr: dict, values: dict) -> Any:
        for name in expr.get("inputs", []):
            if not is_empty(values.get(name)):
                return values[name]
        return None

    # ---- phone_kind：固话/手机分类，配合 case 可驱动通知状态 ----
    def _expr_phone_kind(self, expr: dict, values: dict) -> str:
        text = self._to_text(values.get(expr.get("field"))).replace(" ", "")
        labels = {
            "mobile": expr.get("mobile", "手机"),
            "landline": expr.get("landline", "固话"),
            "unknown": expr.get("unknown", "未知"),
        }
        if MOBILE_RE.match(text):
            return labels["mobile"]
        if LANDLINE_RE.match(text):
            return labels["landline"]
        return labels["unknown"]

    # ---- case：条件映射，按 branches 顺序首命中 ----
    def _expr_case(self, expr: dict, values: dict) -> Any:
        field = expr.get("field")
        actual = values.get(field)
        branches = expr.get("branches", [])
        if not isinstance(branches, list):
            raise LinkageError("case 的 branches 必须是列表")
        for branch in branches:
            if not isinstance(branch, dict) or "value" not in branch:
                raise LinkageError(f"case 分支非法: {branch!r}")
            if "equals" in branch and actual == branch["equals"]:
                return branch["value"]
            if "pattern" in branch and isinstance(actual, str) and re.search(
                str(branch["pattern"]), actual
            ):
                return branch["value"]
        if "default" not in expr:
            raise LinkageError(f"case({field}) 未命中且未配置 default")
        return expr["default"]

    # ---- date_diff：两个日期字段的天数差（end - start） ----
    def _expr_date_diff(self, expr: dict, values: dict) -> int:
        start = self._to_date(values.get(expr.get("start")))
        end = self._to_date(values.get(expr.get("end")))
        if start is None or end is None:
            raise LinkageError("date_diff 的输入不是有效日期")
        return (end - start).days

    @staticmethod
    def _to_text(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    @staticmethod
    def _to_date(value: Any) -> date | None:
        if isinstance(value, date):
            return value
        try:
            parsed = date_parser.parse(str(value))
        except (ValueError, TypeError, OverflowError):
            return None
        return parsed.date()
