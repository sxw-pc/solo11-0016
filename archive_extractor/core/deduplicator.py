"""精确主键与编辑距离模糊去重。"""
from __future__ import annotations

import json
from typing import Any, Optional

from archive_extractor.utils.common import utc_now


def levenshtein(left: str, right: str) -> int:
    """经典动态规划编辑距离；先剥离公共前后缀，降低批量去重开销。"""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    prefix = 0
    limit = min(len(left), len(right))
    while prefix < limit and left[prefix] == right[prefix]:
        prefix += 1
    if prefix:
        left, right = left[prefix:], right[prefix:]
    suffix = 0
    limit = min(len(left), len(right))
    while suffix < limit and left[-1 - suffix] == right[-1 - suffix]:
        suffix += 1
    if suffix:
        left, right = left[:-suffix], right[:-suffix]
    if not left:
        return len(right)
    if not right:
        return len(left)
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, 1):
        current = [i]
        for j, right_char in enumerate(right, 1):
            cost = 0 if left_char == right_char else 1
            current.append(
                min(current[j - 1] + 1, previous[j] + 1, previous[j - 1] + cost)
            )
        previous = current
    return previous[-1]


def similarity(left: str, right: str) -> float:
    """归一化相似度，1 表示完全一致。"""
    size = max(len(left), len(right))
    if size == 0:
        return 1.0
    return 1.0 - levenshtein(left, right) / size


def _nested(data: dict, path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class Deduplicator:
    """在单个任务范围内识别重复结果，逻辑删除而不物理删除。"""

    def __init__(self, db, default_threshold: float = 0.9) -> None:
        self.db = db
        self.default_threshold = float(default_threshold)
        self._cache: dict[int, dict[int, dict]] = {}

    def begin(self, task_id: int) -> None:
        """任务级预加载候选索引，避免每个文件重复读取全表。"""
        rows = self.db.query(
            """SELECT id, data_json, source_mtime FROM results
               WHERE task_id = ? AND is_deleted = 0
               AND status IN ('valid', 'warning')""",
            (task_id,),
        )
        self._cache[task_id] = {
            row["id"]: {
                "id": row["id"],
                "data": json.loads(row["data_json"]),
                "source_mtime": row["source_mtime"],
            }
            for row in rows
        }

    def clear_cache(self, task_id: int) -> None:
        """释放任务级候选索引。"""
        self._cache.pop(task_id, None)

    def _candidates(self, task_id: int, result_id: int) -> list[dict]:
        if task_id in self._cache:
            return [
                item for item_id, item in self._cache[task_id].items()
                if item_id != result_id
            ]
        rows = self.db.query(
            """SELECT id, data_json, source_mtime FROM results
               WHERE task_id = ? AND id <> ? AND is_deleted = 0
               AND status IN ('valid', 'warning')""",
            (task_id, result_id),
        )
        for row in rows:
            row["data"] = json.loads(row["data_json"])
        return rows

    def _update_cache(self, task_id: int, result_id: int, data: dict,
                      source_mtime: float, loser: Optional[int],
                      keeper: int) -> None:
        cache = self._cache.get(task_id)
        if cache is None:
            return
        if loser is not None:
            cache.pop(loser, None)
        if keeper == result_id:
            cache[result_id] = {
                "id": result_id, "data": data, "source_mtime": source_mtime,
            }

    def find_duplicate(
        self, data: dict, candidates: list[dict], config: dict
    ) -> tuple[Optional[dict], str, float]:
        """返回 (命中的已有记录, 原因, 相似度)。"""
        keys = config.get("primary_key", [])
        for candidate in candidates:
            if keys and all(
                _nested(data, key) == _nested(candidate["data"], key)
                for key in keys
            ):
                return candidate, "primary_key", 1.0
        fields = config.get("fuzzy_fields", [])
        threshold = float(config.get("threshold", self.default_threshold))
        if not fields:
            return None, "", 0.0
        left_values = [str(_nested(data, key) or "") for key in fields]
        best, best_score = None, 0.0
        for candidate in candidates:
            score = 1.0
            for key, left in zip(fields, left_values):
                right = str(_nested(candidate["data"], key) or "")
                if not left and not right:
                    continue
                longest = max(len(left), len(right))
                if longest and min(len(left), len(right)) / longest < threshold:
                    score = 0.0
                    break
                score = similarity(left, right)
                if score < threshold:
                    break
            if score > best_score:
                best, best_score = candidate, score
            if best_score >= 1.0:
                break
        if best is not None and best_score >= threshold:
            return best, "fuzzy_content", round(best_score, 4)
        return None, "", round(best_score, 4)

    def apply(
        self,
        task_id: int,
        result_id: int,
        data: dict,
        source_mtime: float,
        config: dict,
    ) -> Optional[dict]:
        """检查新结果是否重复，保留修改时间较新的文件对应记录。"""
        candidate, reason, score = self.find_duplicate(
            data, self._candidates(task_id, result_id), config
        )
        if not candidate:
            self._update_cache(task_id, result_id, data, source_mtime, None,
                               result_id)
            return None
        keep_new = float(source_mtime) >= float(candidate.get("source_mtime") or 0)
        loser = candidate["id"] if keep_new else result_id
        keeper = result_id if keep_new else candidate["id"]
        self._update_cache(task_id, result_id, data, source_mtime, loser, keeper)
        self.db.execute(
            """UPDATE results SET is_deleted = 1, status = 'duplicate',
               duplicate_of = ?, deleted_reason = ?, updated_at = ?
               WHERE id = ?""",
            (keeper, reason, utc_now(), loser),
        )
        report = {
            "task_id": task_id,
            "result_id": loser,
            "duplicate_of": keeper,
            "reason": reason,
            "similarity": score,
            "action": "logical_delete",
            "created_at": utc_now(),
        }
        self.db.add_dedup_report(report)
        return report

    def restore(self, result_id: int) -> bool:
        """恢复被逻辑删除的重复记录。"""
        row = self.db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
        if not row or not row.get("is_deleted"):
            return False
        self._cache.pop(row.get("task_id"), None)
        self.db.execute(
            """UPDATE results SET is_deleted = 0, status = 'valid',
               duplicate_of = NULL, deleted_reason = NULL, updated_at = ?
               WHERE id = ?""",
            (utc_now(), result_id),
        )
        return True

    def report(self, task_id: int) -> list[dict]:
        """查询任务去重报告。"""
        return self.db.query(
            """SELECT d.*, r.source_rel_path FROM dedup_reports d
               LEFT JOIN results r ON r.id = d.result_id
               WHERE d.task_id = ? ORDER BY d.id""",
            (task_id,),
        )
