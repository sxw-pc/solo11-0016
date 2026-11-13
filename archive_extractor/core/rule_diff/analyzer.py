"""规则结构对比：把新旧规则差异归因到选择器、转换逻辑或校验阈值。

本模块只做静态结构分析，不读取任何 HTML，也不触碰数据库。
归因标签约定：

- ``selector``  选择器/匹配方式调整（selector、selector_type、attr、all、
  group、match 标记、字段新增消失导致的命中变化）
- ``transform`` 转换逻辑调整（type、item_type、trim、remove_whitespace、
  replacements、default 等预处理与类型转换配置）
- ``validation`` 校验阈值/等级调整（required、validators、跨字段 validations）
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional

SELECTOR_KEYS = {"selector", "selector_type", "attr", "all", "group"}
TRANSFORM_KEYS = {
    "type", "item_type", "trim", "remove_whitespace", "replacements",
    "default",
}
VALIDATION_KEYS = {"required", "validators"}

CAUSE_SELECTOR = "selector"
CAUSE_TRANSFORM = "transform"
CAUSE_VALIDATION = "validation"
CAUSE_ORDER = (CAUSE_SELECTOR, CAUSE_TRANSFORM, CAUSE_VALIDATION)


def _index_rules(ruleset) -> dict[str, dict]:
    return {rule["rule_id"]: rule for rule in ruleset.all()}


def _field_map(rule: Optional[dict]) -> dict[str, dict]:
    if not rule:
        return {}
    return dict(rule.get("fields", {}))


def _classify_key(key: str) -> str:
    if key in SELECTOR_KEYS:
        return CAUSE_SELECTOR
    if key in VALIDATION_KEYS:
        return CAUSE_VALIDATION
    if key in TRANSFORM_KEYS:
        return CAUSE_TRANSFORM
    # label 等展示性配置变化不影响提取结果，仍归入转换类以便人工知悉
    return CAUSE_TRANSFORM


def _changed_keys(left: dict, right: dict) -> set[str]:
    keys = set(left) | set(right)
    return {key for key in keys if left.get(key) != right.get(key)}


def _object_child_changes(
    old_field: dict, new_field: dict, prefix: str
) -> list[dict]:
    changes: list[dict] = []
    old_children = old_field.get("fields", {}) or {}
    new_children = new_field.get("fields", {}) or {}
    for name in sorted(set(old_children) | set(new_children)):
        full_name = f"{prefix}.{name}"
        old_child = old_children.get(name)
        new_child = new_children.get(name)
        if old_child is None:
            changes.append({
                "field": full_name, "change": "field_added",
                "causes": [CAUSE_SELECTOR],
                "detail": "新增对象子字段",
            })
            continue
        if new_child is None:
            changes.append({
                "field": full_name, "change": "field_removed",
                "causes": [CAUSE_SELECTOR],
                "detail": "删除对象子字段",
            })
            continue
        for key in sorted(_changed_keys(old_child, new_child)):
            changes.append({
                "field": full_name,
                "change": "config_changed",
                "causes": [_classify_key(key)],
                "detail": {
                    "config_key": key,
                    "old": deepcopy(old_child.get(key)),
                    "new": deepcopy(new_child.get(key)),
                },
            })
    return changes


def _diff_field(
    name: str, old_field: Optional[dict], new_field: Optional[dict]
) -> list[dict]:
    """对比同名字段，返回带归因标签的结构变化列表。"""
    if old_field is None:
        return [{
            "field": name, "change": "field_added",
            "causes": [CAUSE_SELECTOR],
            "detail": "新规则新增字段",
        }]
    if new_field is None:
        return [{
            "field": name, "change": "field_removed",
            "causes": [CAUSE_SELECTOR],
            "detail": "新规则删除字段",
        }]
    changes: list[dict] = []
    for key in sorted(_changed_keys(old_field, new_field)):
        if key == "fields" and old_field.get("type") == "object":
            changes.extend(_object_child_changes(old_field, new_field, name))
            continue
        changes.append({
            "field": name,
            "change": "config_changed",
            "causes": [_classify_key(key)],
            "detail": {
                "config_key": key,
                "old": deepcopy(old_field.get(key)),
                "new": deepcopy(new_field.get(key)),
            },
        })
    return changes


def _diff_rule_entry(
    rule_id: str, old_rule: Optional[dict], new_rule: Optional[dict]
) -> dict:
    entry: dict[str, Any] = {
        "rule_id": rule_id,
        "status": "unchanged",
        "field_changes": [],
        "match_changed": False,
        "validation_changes": [],
        "dedup_changed": False,
    }
    if old_rule is None:
        entry["status"] = "rule_added"
        entry["field_changes"] = [
            {
                "field": name, "change": "field_added",
                "causes": [CAUSE_SELECTOR], "detail": "随新规则新增",
            }
            for name in sorted(_field_map(new_rule))
        ]
        return entry
    if new_rule is None:
        entry["status"] = "rule_removed"
        entry["field_changes"] = [
            {
                "field": name, "change": "field_removed",
                "causes": [CAUSE_SELECTOR], "detail": "随规则删除",
            }
            for name in sorted(_field_map(old_rule))
        ]
        return entry
    if old_rule.get("match") != new_rule.get("match"):
        entry["match_changed"] = True
    old_fields, new_fields = _field_map(old_rule), _field_map(new_rule)
    for name in sorted(set(old_fields) | set(new_fields)):
        entry["field_changes"].extend(
            _diff_field(name, old_fields.get(name), new_fields.get(name))
        )
    if old_rule.get("validations") != new_rule.get("validations"):
        entry["validation_changes"] = {
            "old": deepcopy(old_rule.get("validations", [])),
            "new": deepcopy(new_rule.get("validations", [])),
        }
    if old_rule.get("dedup") != new_rule.get("dedup"):
        entry["dedup_changed"] = True
    if (
        entry["field_changes"]
        or entry["match_changed"]
        or entry["validation_changes"]
        or entry["dedup_changed"]
    ):
        entry["status"] = "changed"
    return entry


def analyze_rules(old_ruleset, new_ruleset) -> dict:
    """对比两个 RuleSet，返回逐规则、逐字段的结构变化与归因。"""
    old_index = _index_rules(old_ruleset)
    new_index = _index_rules(new_ruleset)
    rule_ids = sorted(set(old_index) | set(new_index))
    rules = [
        _diff_rule_entry(rule_id, old_index.get(rule_id), new_index.get(rule_id))
        for rule_id in rule_ids
    ]
    # 字段名 -> 归因标签集合，供数据比对阶段快速判定变化根源
    field_causes: dict[str, set[str]] = {}
    for entry in rules:
        for change in entry["field_changes"]:
            field_causes.setdefault(change["field"], set()).update(
                change["causes"]
            )
    affected_fields = {
        field: sorted(causes, key=CAUSE_ORDER.index)
        for field, causes in sorted(field_causes.items())
    }
    match_changed_rules = [
        entry["rule_id"] for entry in rules if entry["match_changed"]
    ]
    return {
        "old_signature": old_ruleset.signature,
        "new_signature": new_ruleset.signature,
        "old_versions": _version_payload(old_ruleset),
        "new_versions": _version_payload(new_ruleset),
        "rules": rules,
        "affected_fields": affected_fields,
        "match_changed_rules": match_changed_rules,
    }


def _version_payload(ruleset) -> list[dict]:
    return [
        {
            "rule_id": rule["rule_id"],
            "version": rule["_effective_version"],
            "content_hash": rule["_content_hash"],
            "declared_version": rule.get("version", ""),
        }
        for rule in ruleset.all()
    ]


def causes_for_field(analysis: dict, field: str) -> list[str]:
    """返回某字段在规则层面的归因标签；无结构变化时返回空列表。"""
    return list(analysis.get("affected_fields", {}).get(field, []))
