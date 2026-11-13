"""两份隔离试跑台账的字段级差异比对。

比对维度（按验收要求）：

- ``field_added``    新规则台账中出现的新字段
- ``field_removed``  旧规则台账中存在、新规则消失的字段
- ``value_changed``  同名字段提取值变化
- ``level_changed``  字段值未变但校验等级变化（含整体结果等级变化）

每条字段差异附带归因标签（selector / transform / validation），
归因来自 :mod:`analyzer` 的静态规则结构分析；
同时汇总受影响源文件清单及各自 MD5，供回滚判断。
"""
from __future__ import annotations

from typing import Any, Optional

from archive_extractor.core.rule_diff.analyzer import (
    CAUSE_SELECTOR,
    CAUSE_VALIDATION,
    causes_for_field,
)

FIELD_ADDED = "field_added"
FIELD_REMOVED = "field_removed"
VALUE_CHANGED = "value_changed"
LEVEL_CHANGED = "level_changed"
STATUS_CHANGED = "extraction_status_changed"
RULE_MATCH_CHANGED = "rule_match_changed"


def _flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """把 object 字段展开为点号路径，数组与基本类型保持整体比较。"""
    flat: dict[str, Any] = {}
    if not isinstance(data, dict):
        return flat
    for key, value in data.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            nested = _flatten(value, name)
            if nested:
                flat.update(nested)
            else:
                flat[name] = value
        else:
            flat[name] = value
    return flat


def _field_levels(record: dict) -> dict[str, str]:
    """取每个字段在校验结果中的最高等级；无问题字段不在映射中。"""
    order = {"info": 0, "warning": 1, "error": 2}
    levels: dict[str, str] = {}
    for issue in record.get("validations", []):
        field = issue.get("field", "")
        level = issue.get("level", "info")
        if field not in levels or order.get(level, -1) > order.get(
            levels[field], -1
        ):
            levels[field] = level
    return levels


def _resolve_causes(
    analysis: dict, field: str, record: dict, fallback: Optional[str] = None
) -> list[str]:
    causes = causes_for_field(analysis, field)
    if not causes and fallback:
        causes = [fallback]
    if not causes and record.get("rule_match_changed"):
        causes = [CAUSE_SELECTOR]
    return causes


def _diff_record(
    rel_path: str,
    old_record: Optional[dict],
    new_record: Optional[dict],
    analysis: dict,
    rule_validation_changed: bool,
) -> Optional[dict]:
    """比对单个源文件在两套规则下的试跑结果。"""
    entry: dict[str, Any] = {
        "source_rel_path": rel_path,
        "source_md5_old": (old_record or {}).get("source_md5"),
        "source_md5_new": (new_record or {}).get("source_md5"),
        "change_types": [],
        "field_diffs": [],
        "old_status": (old_record or {}).get("status"),
        "new_status": (new_record or {}).get("status"),
        "old_error_level": (old_record or {}).get("error_level", ""),
        "new_error_level": (new_record or {}).get("error_level", ""),
        "old_rule_id": (old_record or {}).get("rule_id"),
        "new_rule_id": (new_record or {}).get("rule_id"),
        "old_rule_version": (old_record or {}).get("rule_version"),
        "new_rule_version": (new_record or {}).get("rule_version"),
        "old_extract_error": (old_record or {}).get("extract_error"),
        "new_extract_error": (new_record or {}).get("extract_error"),
    }
    # 一侧提取失败（未匹配规则/解析失败等）的情况
    old_failed = old_record is None or old_record.get("status") == "failed"
    new_failed = new_record is None or new_record.get("status") == "failed"
    if old_failed or new_failed:
        if old_failed != new_failed or (
            old_record and new_record
            and old_record.get("extract_error") != new_record.get("extract_error")
        ):
            entry["change_types"].append(STATUS_CHANGED)
    if (
        old_record and new_record
        and old_record.get("rule_id") != new_record.get("rule_id")
    ):
        entry["change_types"].append(RULE_MATCH_CHANGED)
    entry["rule_match_changed"] = RULE_MATCH_CHANGED in entry["change_types"]

    old_flat = _flatten((old_record or {}).get("data", {}))
    new_flat = _flatten((new_record or {}).get("data", {}))
    old_levels = _field_levels(old_record or {})
    new_levels = _field_levels(new_record or {})
    all_fields = sorted(set(old_flat) | set(new_flat))

    for field in all_fields:
        in_old = field in old_flat
        in_new = field in new_flat
        if in_old and not in_new:
            change = FIELD_REMOVED
            causes = _resolve_causes(analysis, field, entry)
            diff = {
                "field": field, "change": change, "causes": causes,
                "old_value": old_flat[field], "new_value": None,
                "old_level": old_levels.get(field, ""),
                "new_level": "",
            }
        elif in_new and not in_old:
            change = FIELD_ADDED
            causes = _resolve_causes(analysis, field, entry)
            diff = {
                "field": field, "change": change, "causes": causes,
                "old_value": None, "new_value": new_flat[field],
                "old_level": "",
                "new_level": new_levels.get(field, ""),
            }
        elif old_flat[field] != new_flat[field]:
            change = VALUE_CHANGED
            causes = _resolve_causes(analysis, field, entry)
            diff = {
                "field": field, "change": change, "causes": causes,
                "old_value": old_flat[field], "new_value": new_flat[field],
                "old_level": old_levels.get(field, ""),
                "new_level": new_levels.get(field, ""),
            }
        else:
            old_level = old_levels.get(field, "")
            new_level = new_levels.get(field, "")
            if old_level != new_level:
                change = LEVEL_CHANGED
                causes = _resolve_causes(
                    analysis, field, entry,
                    fallback=CAUSE_VALIDATION if rule_validation_changed else None,
                )
                diff = {
                    "field": field, "change": change, "causes": causes,
                    "old_value": old_flat[field], "new_value": new_flat[field],
                    "old_level": old_level, "new_level": new_level,
                }
            else:
                continue
        entry["field_diffs"].append(diff)
        if change not in entry["change_types"]:
            entry["change_types"].append(change)

    # 值完全相同，但文件整体校验等级变化（跨字段校验引发）
    if (
        not entry["field_diffs"]
        and entry["old_error_level"] != entry["new_error_level"]
    ):
        entry["change_types"].append(LEVEL_CHANGED)
        entry["field_diffs"].append({
            "field": None,
            "change": LEVEL_CHANGED,
            "causes": [CAUSE_VALIDATION] if rule_validation_changed else [],
            "old_value": None,
            "new_value": None,
            "old_level": entry["old_error_level"],
            "new_level": entry["new_error_level"],
        })

    if not entry["change_types"]:
        return None
    return entry


def compare_ledgers(
    old_ledger: dict,
    new_ledger: dict,
    analysis: dict,
) -> dict:
    """组装完整结构化差异报告。"""
    old_records = old_ledger["records"]
    new_records = new_ledger["records"]
    rule_validation_changed = any(
        rule.get("validation_changes") for rule in analysis.get("rules", [])
    )
    all_paths = sorted(set(old_records) | set(new_records))
    changed_files: list[dict] = []
    unchanged_files: list[str] = []
    counters = {
        FIELD_ADDED: 0, FIELD_REMOVED: 0, VALUE_CHANGED: 0,
        LEVEL_CHANGED: 0, STATUS_CHANGED: 0, RULE_MATCH_CHANGED: 0,
    }
    cause_counter: dict[str, int] = {}
    for rel_path in all_paths:
        entry = _diff_record(
            rel_path,
            old_records.get(rel_path),
            new_records.get(rel_path),
            analysis,
            rule_validation_changed,
        )
        if entry is None:
            unchanged_files.append(rel_path)
            continue
        changed_files.append(entry)
        for change in entry["change_types"]:
            counters[change] = counters.get(change, 0) + 1
        for diff in entry["field_diffs"]:
            for cause in diff.get("causes", []):
                cause_counter[cause] = cause_counter.get(cause, 0) + 1

    affected_files = [
        {
            "rel_path": entry["source_rel_path"],
            "md5": entry["source_md5_new"] or entry["source_md5_old"],
            "md5_old": entry["source_md5_old"],
            "md5_new": entry["source_md5_new"],
            "change_types": entry["change_types"],
        }
        for entry in changed_files
    ]
    return {
        "summary": {
            "total_files": len(all_paths),
            "changed_files": len(changed_files),
            "unchanged_files": len(unchanged_files),
            "field_added": counters[FIELD_ADDED],
            "field_removed": counters[FIELD_REMOVED],
            "value_changed": counters[VALUE_CHANGED],
            "level_changed": counters[LEVEL_CHANGED],
            "extraction_status_changed": counters[STATUS_CHANGED],
            "rule_match_changed": counters[RULE_MATCH_CHANGED],
            "changes_by_cause": cause_counter,
        },
        "rule_analysis": analysis,
        "affected_files": affected_files,
        "changed_file_details": changed_files,
        "unchanged_files": unchanged_files,
    }
