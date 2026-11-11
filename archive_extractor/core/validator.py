"""字段合规校验与可扩展校验函数。"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

from dateutil import parser as date_parser

LEVEL_ORDER = {"info": 0, "warning": 1, "error": 2}
PATTERNS = {
    "phone": r"^1[3-9]\d{9}$",
    "id_card": r"^\d{17}[\dXx]$",
    "credit_code": r"^[0-9A-HJ-NPQRTUWXY]{18}$",
    "email": r"^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$",
    "date": r"^\d{4}-\d{2}-\d{2}$",
}
CUSTOM_VALIDATORS: dict[str, Callable] = {}


@dataclass
class ValidationIssue:
    """一条可定位的校验结果。"""

    level: str
    field: str
    message: str
    validator: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def register_validator(name: str):
    """注册自定义校验函数：func(value, data, config) -> 消息或 (等级, 消息)。"""

    def decorator(func: Callable) -> Callable:
        CUSTOM_VALIDATORS[name] = func
        return func

    return decorator


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _add(
    issues: list[ValidationIssue],
    level: str,
    field: str,
    message: str,
    validator: str,
) -> None:
    issues.append(
        ValidationIssue(level=level, field=field, message=message, validator=validator)
    )


def _match_pattern(value: str, kind: str, config: dict) -> bool:
    pattern = PATTERNS.get(kind, config.get("pattern", ""))
    if not pattern:
        return True
    try:
        return re.match(pattern, value) is not None
    except re.error:
        return False


def _check_validator(
    value: Any, data: dict, config: dict, field: str
) -> Optional[ValidationIssue]:
    kind = str(config.get("type", ""))
    level = str(config.get("level", "error"))
    message = str(config.get("message", f"字段 {field} 校验未通过"))
    if kind == "min_length" and len(str(value)) < int(config.get("value", 0)):
        return ValidationIssue(level, field, message, kind)
    if kind == "max_length" and len(str(value)) > int(config.get("value", 0)):
        return ValidationIssue(level, field, message, kind)
    if kind == "regex" and not _match_pattern(str(value), "", config):
        return ValidationIssue(level, field, message, kind)
    if kind in PATTERNS and not _match_pattern(str(value), kind, config):
        return ValidationIssue(level, field, message, kind)
    if kind == "enum" and value not in config.get("values", []):
        return ValidationIssue(level, field, message, kind)
    if kind == "range":
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ValidationIssue(level, field, message, kind)
        low = config.get("min")
        high = config.get("max")
        if low is not None and number < float(low):
            return ValidationIssue(level, field, message, kind)
        if high is not None and number > float(high):
            return ValidationIssue(level, field, message, kind)
    if kind in CUSTOM_VALIDATORS:
        result = CUSTOM_VALIDATORS[kind](value, data, config)
        if result:
            if isinstance(result, tuple):
                return ValidationIssue(result[0], field, result[1], kind)
            return ValidationIssue(level, field, str(result), kind)
    return None


def _walk_fields(
    fields: dict, data: dict, issues: list[ValidationIssue], prefix: str = ""
) -> None:
    for name, field in fields.items():
        path = f"{prefix}.{name}" if prefix else name
        value = data.get(name) if isinstance(data, dict) else None
        if field.get("type") == "object":
            if field.get("required") and _empty(value):
                _add(issues, "error", path, f"{field.get('label', path)}为必填项", "required")
            if isinstance(value, dict):
                _walk_fields(field.get("fields", {}), value, issues, path)
            continue
        if field.get("required") and _empty(value):
            _add(issues, "error", path, f"{field.get('label', path)}为必填项", "required")
            continue
        if _empty(value):
            continue
        for config in field.get("validators", []):
            issue = _check_validator(value, data, config, path)
            if issue:
                issues.append(issue)


def _compare(left: Any, op: str, right: Any) -> bool:
    if left is None or right is None:
        return True
    try:
        if op in {">", ">=", "<", "<="}:
            lhs, rhs = float(left), float(right)
        else:
            lhs, rhs = left, right
    except (TypeError, ValueError):
        return True
    return {
        ">": lhs > rhs,
        ">=": lhs >= rhs,
        "<": lhs < rhs,
        "<=": lhs <= rhs,
        "==": lhs == rhs,
        "!=": lhs != rhs,
    }.get(op, True)


def _parse_date(value: Any):
    if value is None or value == "":
        return None
    try:
        return date_parser.parse(str(value)).date()
    except (ValueError, TypeError, OverflowError):
        return None


def _cross_validation(
    item: dict, data: dict, issues: list[ValidationIssue]
) -> None:
    kind = item.get("type")
    level = item.get("level", "error")
    message = item.get("message", "关联字段逻辑校验未通过")
    if kind == "date_order":
        start = _parse_date(data.get(item.get("start")))
        end = _parse_date(data.get(item.get("end")))
        if start and end and end < start:
            _add(issues, level, f"{item.get('start')}~{item.get('end')}", message, kind)
    elif kind == "compare":
        left = data.get(item.get("left"))
        right = item.get("right")
        if item.get("right_field"):
            right = data.get(item.get("right_field"))
        if not _compare(left, str(item.get("op", "==")), right):
            _add(issues, level, item.get("left", ""), message, kind)


def validate_data(rule: dict, data: dict) -> list[ValidationIssue]:
    """校验提取数据，返回全部错误/警告/提示。"""
    issues: list[ValidationIssue] = []
    _walk_fields(rule.get("fields", {}), data or {}, issues)
    for item in rule.get("validations", []):
        _cross_validation(item, data or {}, issues)
    return issues


def highest_level(issues: list[ValidationIssue]) -> str:
    """返回最高严重等级，无问题时返回空字符串。"""
    if not issues:
        return ""
    return max(issues, key=lambda item: LEVEL_ORDER.get(item.level, -1)).level


def issues_to_dicts(issues: list[ValidationIssue]) -> list[dict]:
    return [issue.to_dict() for issue in issues]
