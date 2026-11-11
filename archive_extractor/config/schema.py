"""YAML 提取规则的结构定义与校验。"""
from __future__ import annotations

import re
from typing import Any

from archive_extractor.utils.common import RuleError

VALID_TYPES = {
    "text",
    "number",
    "date",
    "boolean",
    "enum",
    "object",
    "array",
}
VALID_SELECTORS = {"css", "xpath", "regex"}
VALID_LEVELS = {"error", "warning", "info"}
FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
BUILTIN_VALIDATORS = {
    "required",
    "regex",
    "min_length",
    "max_length",
    "enum",
    "range",
    "phone",
    "id_card",
    "credit_code",
    "email",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuleError(message)


def normalize_selector_type(field: dict) -> str:
    """兼容 selector_type 与简写 type 的提取方式。"""
    selector_type = field.get("selector_type", "css")
    return str(selector_type).lower()


def validate_fields(fields: Any, prefix: str = "") -> None:
    """递归校验字段定义。"""
    _require(isinstance(fields, dict) and bool(fields), f"{prefix}字段定义不能为空")
    for name, field in fields.items():
        path = f"{prefix}.{name}" if prefix else name
        _require(bool(FIELD_NAME_RE.match(str(name))), f"非法字段名: {path}")
        _require(isinstance(field, dict), f"字段 {path} 必须是对象")
        field_type = field.get("type", "text")
        _require(field_type in VALID_TYPES, f"字段 {path} 类型不支持: {field_type}")
        selector_type = normalize_selector_type(field)
        _require(selector_type in VALID_SELECTORS, f"{path} 选择器类型不支持")
        if field_type == "object":
            validate_fields(field.get("fields", {}), path)
        elif field_type == "array":
            item_type = field.get("item_type", "text")
            _require(item_type in VALID_TYPES - {"object"}, f"{path} 数组项类型不支持")
        if "selector" not in field and "default" not in field:
            raise RuleError(f"字段 {path} 缺少 selector 或 default")
        validators = field.get("validators", [])
        _require(isinstance(validators, list), f"{path}.validators 必须是列表")
        for rule in validators:
            _require(isinstance(rule, dict), f"{path} 校验项必须是对象")
            level = rule.get("level", "error")
            _require(level in VALID_LEVELS, f"{path} 校验等级非法")
            _validate_validation(rule, path)


def _validate_validation(rule: dict, path: str) -> None:
    kind = rule.get("type")
    _require(bool(kind), f"{path} 校验项缺少 type")
    if kind == "regex":
        _require(bool(rule.get("pattern")), f"{path} 正则校验缺少 pattern")
        try:
            re.compile(str(rule["pattern"]))
        except re.error as exc:
            raise RuleError(f"{path} 正则表达式错误: {exc}") from exc
    if kind == "enum":
        _require(isinstance(rule.get("values"), list), f"{path} 枚举校验缺少 values")


def validate_rule(rule: dict) -> dict:
    """校验单条规则并补齐默认值，返回原对象。"""
    _require(isinstance(rule, dict), "规则文件根节点必须是对象")
    for key in ("rule_id", "name", "version", "match", "fields"):
        _require(key in rule, f"规则缺少必填项: {key}")
    _require(
        re.match(r"^[a-z][a-z0-9_]{1,63}$", str(rule["rule_id"])) is not None,
        f"rule_id 不合法: {rule['rule_id']}",
    )
    match = rule["match"]
    _require(isinstance(match, dict), "match 必须是对象")
    _require(
        bool(match.get("form_selector") or match.get("marker")),
        "match 至少配置 form_selector 或 marker",
    )
    validate_fields(rule["fields"])
    dedup = rule.get("dedup", {})
    _require(isinstance(dedup, dict), "dedup 必须是对象")
    if dedup:
        _require(
            isinstance(dedup.get("primary_key"), list),
            "dedup.primary_key 必须是列表",
        )
        threshold = dedup.get("threshold", 0.9)
        _require(0 <= float(threshold) <= 1, "去重阈值必须在 0~1 之间")
    validations = rule.get("validations", [])
    _require(isinstance(validations, list), "validations 必须是列表")
    for item in validations:
        _require(isinstance(item, dict), "跨字段校验项必须是对象")
        _require(item.get("type") in {"date_order", "compare"}, "跨字段校验类型不支持")
        level = item.get("level", "error")
        _require(level in VALID_LEVELS, "跨字段校验等级非法")
    rule.setdefault("form_type", rule["rule_id"])
    rule.setdefault("description", "")
    rule.setdefault("validations", [])
    return rule
