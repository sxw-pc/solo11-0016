"""溯源链生成、快照与版本对比。"""
from __future__ import annotations

import difflib
from typing import Any

from archive_extractor.utils.common import utc_now


class Tracer:
    """负责生成溯源记录并查询完整链路。"""

    def __init__(self, db, file_store) -> None:
        self.db = db
        self.file_store = file_store

    def make_snapshot(self, source_path: str, md5: str, original_name: str) -> str:
        """首次处理文件时创建快照；同一哈希复用。"""
        return self.file_store.snapshot(source_path, md5, original_name)

    def record(
        self, result_id: int, traces: list[dict], source_md5: str, rule_version: str
    ) -> None:
        """批量写入字段级溯源，包含行号、选择器与规则版本。

        普通字段溯源与联动溯源共用一张表；联动行额外携带上游字段、
        表达式与联动状态，未提供这些键时按普通字段行写入，老调用方无感。
        """
        self.db.executemany(
            """INSERT INTO traces
               (result_id, field, selector, selector_type, source_line,
                raw_value, extracted_at, rule_version, source_md5,
                derived, upstream_fields, expression, linkage_status,
                linkage_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    result_id,
                    trace["field"],
                    trace.get("selector", ""),
                    trace.get("selector_type", "css"),
                    trace["source_line"],
                    trace["raw_value"],
                    trace.get("extracted_at", utc_now()),
                    trace.get("rule_version", rule_version),
                    trace.get("source_md5", source_md5),
                    int(trace.get("derived", 0)),
                    trace.get("upstream_fields", ""),
                    trace.get("expression", ""),
                    trace.get("linkage_status", ""),
                    trace.get("linkage_reason", ""),
                )
                for trace in traces
            ],
        )

    def chain(self, result_id: int) -> dict:
        """返回结果、校验、溯源、快照和历史版本的完整链条。"""
        result = self.db.result_detail(result_id)
        if not result:
            return {}
        traces = self.db.query(
            "SELECT * FROM traces WHERE result_id = ? ORDER BY id", (result_id,)
        )
        validations = self.db.query(
            "SELECT * FROM validations WHERE result_id = ? ORDER BY id", (result_id,)
        )
        history = self.db.query(
            """SELECT id, version, source_md5, status, error_level, created_at
               FROM results WHERE task_id = ? AND source_rel_path = ?
               ORDER BY version""",
            (result["task_id"], result["source_rel_path"]),
        )
        return {
            "result": result,
            "traces": traces,
            "validations": validations,
            "history": history,
            "snapshot": result.get("snapshot_path", ""),
        }

    def compare_versions(self, left_id: int, right_id: int) -> list[str]:
        """比较同一文件两个结果版本的数据差异。"""
        left = self.db.get_result(left_id)
        right = self.db.get_result(right_id)
        if not left or not right:
            return []
        import json

        left_data = json.dumps(
            json.loads(left["data_json"]), ensure_ascii=False, indent=2
        ).splitlines()
        right_data = json.dumps(
            json.loads(right["data_json"]), ensure_ascii=False, indent=2
        ).splitlines()
        return list(
            difflib.unified_diff(
                left_data,
                right_data,
                fromfile=f"result-{left_id}",
                tofile=f"result-{right_id}",
                lineterm="",
            )
        )

    def snapshot_info(self, result_id: int) -> dict[str, Any]:
        """返回快照绝对路径与文件大小，供管理端预览。"""
        result = self.db.get_result(result_id)
        if not result or not result.get("snapshot_path"):
            return {}
        path = self.file_store.resolve(result["snapshot_path"])
        if not path.exists():
            return {}
        return {"path": str(path), "size": path.stat().st_size}
