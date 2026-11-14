"""联动局部重算：不重解析源文件，仅刷新受影响的派生字段与约束。"""
from __future__ import annotations

import json

from archive_extractor.config.loader import load_rules
from archive_extractor.core.linkage import LinkageEngine
from archive_extractor.core.validator import (
    ValidationIssue,
    highest_level,
    issues_to_dicts,
    validate_data,
)
from archive_extractor.utils.common import utc_now


def _affected_fields(changed_fields, graph):
    """受影响字段集合：变化字段本身、其全部下游派生字段、相关约束引用字段。"""
    affected = set(changed_fields)
    for field in changed_fields:
        affected.update(graph.affected_by(field))
        for constraint in graph.constraints_touching(field):
            affected.update(constraint.get("_refs", ()))
    return affected


def _revalidate_affected(rule, data, old_validations, affected):
    """只重跑受影响字段的字段级校验，未受影响字段沿用旧版本校验结论。"""
    fresh = validate_data(rule, data)
    kept = [
        ValidationIssue(
            row["level"], row["field"], row["message"], row["validator"]
        )
        for row in old_validations
        if row["field"] not in affected
        and row["validator"] not in {"linkage_constraint", "linkage_derivation"}
    ]
    refreshed = [issue for issue in fresh if issue.field in affected]
    return kept + refreshed


def rederive_result(runner, task_id: int, rel_path: str,
                    changed_fields: list[str]) -> dict:
    """按依赖图局部重算单条结果，未受影响的字段与溯源保持不动。

    重算会产生新的 result 版本：普通字段溯源从旧版本复制，
    联动溯源（derivation/constraint）按本次影响面整体替换。
    """
    db, tracer = runner.db, runner.tracer
    ruleset = load_rules(runner._task(task_id)["rules_dir"] or None)
    current = db.query_one(
        """SELECT * FROM results WHERE task_id = ? AND source_rel_path = ?
           AND is_deleted = 0 ORDER BY version DESC LIMIT 1""",
        (task_id, rel_path),
    )
    if not current:
        raise ValueError(f"未找到有效结果: {rel_path}")
    rule = ruleset.get(current["rule_id"])
    graph = rule["_linkage_graph"]
    unknown = [
        name for name in changed_fields
        if name not in rule["fields"] and name not in graph.derived_fields
    ]
    if unknown:
        raise ValueError(f"字段不属于该规则: {', '.join(unknown)}")
    engine = LinkageEngine(rule, graph)
    data = json.loads(current["data_json"])
    previous_states = db.linkage_states(current["id"])
    outcome = engine.rederive(
        data, changed_fields, rule["_effective_version"], previous_states
    )
    if not outcome["recomputed"] and not outcome["issues"]:
        return {"recomputed": [], "result_id": current["id"], "changed": False}
    affected = _affected_fields(changed_fields, graph)
    old_validations = db.query(
        "SELECT * FROM validations WHERE result_id = ? ORDER BY id",
        (current["id"],),
    )
    issues = _revalidate_affected(rule, data, old_validations, affected)
    issues.extend(
        ValidationIssue(
            item["level"], item["field"], item["message"], item["validator"]
        )
        for item in outcome["issues"]
    )
    level = highest_level(issues)
    status = "invalid" if level == "error" else (
        "warning" if level == "warning" else "valid"
    )
    with runner._persist_lock:
        version = db.next_result_version(task_id, rel_path)
        db.execute(
            """UPDATE results SET status = 'superseded', is_deleted = 1,
               deleted_reason = 'new_version', updated_at = ? WHERE id = ?""",
            (utc_now(), current["id"]),
        )
        result_id = db.store_result({
            "task_id": task_id, "file_id": current["file_id"],
            "form_type": current["form_type"], "rule_id": current["rule_id"],
            "rule_version": rule["_effective_version"],
            "rule_content_hash": rule["_content_hash"],
            "data": data, "status": status, "error_level": level,
            "source_rel_path": rel_path, "source_md5": current["source_md5"],
            "source_mtime": current["source_mtime"],
            "snapshot_path": current["snapshot_path"], "version": version,
        })
        db.add_validations(result_id, issues_to_dicts(issues))
        kept = db.query(
            """SELECT * FROM traces WHERE result_id = ?
               AND (derived IS NULL OR derived = 0
                    OR selector_type NOT IN ('derivation', 'constraint'))""",
            (current["id"],),
        )
        copied = [
            {
                "field": row["field"], "selector": row["selector"],
                "selector_type": row["selector_type"],
                "source_line": row["source_line"],
                "raw_value": row["raw_value"],
                "extracted_at": row["extracted_at"],
                "rule_version": row["rule_version"],
                "source_md5": row["source_md5"],
            }
            for row in kept
        ]
        for trace in outcome["traces"]:
            trace.setdefault("source_md5", current["source_md5"])
        tracer.record(
            result_id, copied + outcome["traces"],
            current["source_md5"], rule["_effective_version"],
        )
    if current["file_id"]:
        db.update_task_file(
            current["file_id"], result_id=result_id,
            status="success", error=None,
        )
    return {
        "recomputed": outcome["recomputed"],
        "result_id": result_id, "changed": True, "status": status,
    }
