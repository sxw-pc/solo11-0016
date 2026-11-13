"""失败原因分类、隔离台账聚合与遗留记录兼容。

失败可能发生在扫描、读取/解码、HTML 结构解析、规则匹配或处理阶段。本模块把各
阶段抛出的异常映射为稳定的 ``reason`` 代码与 ``category`` 分类，供隔离台账落账、
批量诊断和只读复检复用。分类不依赖异常文本，避免日志措辞变化导致原因漂移。
"""
from __future__ import annotations

from dataclasses import dataclass

CATEGORY_LABELS = {
    "encoding": "编码类",
    "structure": "结构类",
    "rule": "规则匹配类",
    "processing": "处理类",
    "scan": "扫描类",
    "unknown": "未分类",
}

# reason -> (category, stage, 中文说明, 批量诊断提示)
REASON_CATALOG = {
    "stat_failed": ("scan", "scan", "文件状态读取失败", "批量出现时检查目录权限或文件是否被移动"),
    "hash_failed": ("scan", "scan", "文件哈希计算失败", "批量出现时检查磁盘与文件权限"),
    "file_unreadable": ("scan", "read", "文件无法读取", "批量出现时检查文件权限或源目录是否完整"),
    "file_too_large": ("scan", "read", "文件超过大小上限", "检查扫描的大小上限配置"),
    "decode_failed": ("encoding", "encoding", "文件编码无法识别", "同类集中出现时，源文件导出编码可能整体不一致"),
    "empty_content": ("structure", "parse", "HTML 内容为空", "同类集中出现时，检查源文件导出是否被截断"),
    "malformed_html": ("structure", "parse", "HTML 结构无法解析", "同类集中出现时，检查导出流程是否生成了残缺文件"),
    "missing_form_marker": (
        "structure", "match", "缺少表单标记",
        "同类集中出现时，检查导出模板是否丢失 form 标记",
    ),
    "rule_unmatched": (
        "rule", "match", "未匹配到任何已配置规则",
        "同类集中出现时，优先检查规则 form_selector/marker 是否适配整批文件",
    ),
    "invalid_rule_selector": ("rule", "match", "规则选择器配置无效", "检查规则文件中的选择器写法"),
    "timeout": ("processing", "process", "处理超时", "批量出现时检查并发数与单文件超时配置"),
    "unexpected_error": ("processing", "process", "未预期异常", "查看任务日志定位具体堆栈"),
    "recovered": ("unknown", "recheck", "复检已可解析（未入库）", "仅代表本次只读复检通过，需人工确认是否重新执行任务"),
    "legacy_unclassified": ("unknown", "legacy", "历史失败记录（未分类）", "旧版数据库仅有错误文本，可按文本人工复核"),
    "none": ("unknown", "unknown", "无失败", ""),
}

# 旧库 error 文本 -> reason，按顺序匹配；无法识别时归为 legacy_unclassified
LEGACY_HINTS = (
    ("编码", "decode_failed"),
    ("encoding", "decode_failed"),
    ("超时", "timeout"),
    ("timeout", "timeout"),
    ("未匹配", "rule_unmatched"),
    ("form", "missing_form_marker"),
    ("template", "missing_form_marker"),
    ("解析", "malformed_html"),
    ("parse", "malformed_html"),
    ("读取", "file_unreadable"),
    ("为空", "empty_content"),
)


def category_label(category: str) -> str:
    """返回分类的中文标签，未知分类不报错。"""
    return CATEGORY_LABELS.get(category or "unknown", CATEGORY_LABELS["unknown"])


def reason_meta(reason: str) -> tuple[str, str, str, str]:
    """返回 (分类, 阶段, 说明, 诊断提示)，未知原因按未分类处理。"""
    if reason in REASON_CATALOG:
        return REASON_CATALOG[reason]
    return ("unknown", "unknown", "未分类失败", "需人工复核")


@dataclass
class FailureInfo:
    """一次失败的稳定分类结果。"""

    category: str
    stage: str
    reason: str
    message: str

    @property
    def category_label(self) -> str:
        return category_label(self.category)


def build_failure(reason: str, message: str) -> FailureInfo:
    """按原因代码构建分类结果。"""
    category, stage, _, _ = reason_meta(reason)
    return FailureInfo(category, stage, reason, message)


def classify_exception(exc: BaseException) -> FailureInfo:
    """把处理链路中的异常映射为稳定的失败分类。

    优先使用异常自带的 reason（EncodingError/ParseError），其次识别超时，
    最后归为未预期异常。分类只看异常类型与 reason，不看 message 文案。
    """
    reason = getattr(exc, "reason", None)
    if reason:
        return build_failure(reason, str(exc))
    if isinstance(exc, TimeoutError):
        return build_failure("timeout", str(exc) or "处理超时")
    return build_failure("unexpected_error", str(exc) or exc.__class__.__name__)


def infer_legacy_reason(error_text: str) -> str:
    """从旧库仅有的一行错误文本推断原因，推断不出时标记遗留未分类。"""
    text = str(error_text or "").lower()
    for hint, reason in LEGACY_HINTS:
        if hint.lower() in text:
            return reason
    return "legacy_unclassified"


def normalize_legacy_record(row: dict) -> dict:
    """把旧版 task_files 的失败行补齐为新台账结构，缺失字段不抛异常。"""
    reason = row.get("failure_reason") or infer_legacy_reason(row.get("error"))
    category, stage, note, hint = reason_meta(reason)
    item = {
        "id": None,
        "task_id": row.get("task_id"),
        "file_id": row.get("id"),
        "rel_path": row.get("rel_path") or "",
        "category": row.get("failure_category") or category,
        "reason": reason,
        "stage": row.get("failure_stage") or stage,
        "message": row.get("error") or note,
        "source_md5": row.get("md5"),
        "detected_at": row.get("updated_at"),
        "last_seen_at": row.get("updated_at"),
        "occurrences": 1,
        "recheck_status": "isolated",
        "recheck_count": 0,
        "last_recheck_at": row.get("failure_checked_at"),
        "legacy": True,
        "reason_label": note,
        "category_label": category_label(
            row.get("failure_category") or category
        ),
        "batch_hint": hint,
    }
    return item


def _has_ledger(db) -> bool:
    """旧库可能没有 failure_records 表，查询前先确认。"""
    try:
        row = db.query_one(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='failure_records'"
        )
        return bool(row)
    except Exception:
        return False


def collect_isolated(db, task_id: int) -> list[dict]:
    """汇总隔离台账：优先读 failure_records，并把旧库 task_files 失败行补齐。

    旧版数据库没有 failure_records，历史失败只存在于 task_files；这里按
    rel_path 去重合并，保证旧批次仍能展示且同一文件不重复计数。
    """
    normalized: dict[str, dict] = {}
    file_rows = {row["rel_path"]: row for row in db.task_files(task_id)}
    if _has_ledger(db):
        for row in db.failure_rows(task_id):
            item = normalize_legacy_record({})
            item.update({
                "id": row.get("id"),
                "task_id": task_id,
                "file_id": row.get("file_id"),
                "rel_path": row.get("rel_path") or "",
                "category": row.get("category") or "unknown",
                "reason": row.get("reason") or "legacy_unclassified",
                "stage": row.get("stage") or "unknown",
                "message": row.get("message") or "",
                "source_md5": row.get("source_md5"),
                "detected_at": row.get("created_at"),
                "last_seen_at": row.get("updated_at"),
                "occurrences": int(row.get("occurrences") or 1),
                "recheck_status": row.get("status") or "isolated",
                "recheck_count": int(row.get("recheck_count") or 0),
                "last_recheck_at": row.get("last_recheck_at"),
                "source": row.get("source") or "task",
                "legacy": False,
            })
            source_row = file_rows.get(item["rel_path"]) or {}
            item["abs_path"] = source_row.get("abs_path")
            normalized[item["rel_path"]] = _decorate(item)
    for row in db.task_files(task_id, ["failed"]):
        if row["rel_path"] in normalized:
            continue
        item = normalize_legacy_record(row)
        item["task_id"] = task_id
        item["abs_path"] = row.get("abs_path")
        normalized[row["rel_path"]] = _decorate(item)
    return sorted(normalized.values(), key=lambda item: item["rel_path"])


def _decorate(item: dict) -> dict:
    """补齐展示用派生字段，保持旧记录与新记录结构一致。"""
    category = item.get("category") or "unknown"
    reason = item.get("reason") or "legacy_unclassified"
    _, stage, note, hint = reason_meta(reason)
    item["category"] = category
    item["reason"] = reason
    item.setdefault("stage", stage)
    item["category_label"] = category_label(category)
    item["reason_label"] = note
    item["batch_hint"] = hint
    recheck_status = item.get("recheck_status") or "isolated"
    item["recheck_status"] = recheck_status
    item.setdefault("recheck_count", 0)
    item.setdefault("occurrences", 1)
    item.setdefault("legacy", False)
    item["recovered"] = recheck_status == "recovered"
    return item


def summarize(records: list[dict], total_files: int = 0) -> dict:
    """聚合隔离台账，识别“整类规则不匹配”与“个别文件损坏”。"""
    groups: dict[str, dict] = {}
    for record in records:
        category = record.get("category") or "unknown"
        reason = record.get("reason") or "legacy_unclassified"
        group = groups.setdefault(reason, {
            "category": category,
            "category_label": category_label(category),
            "reason": reason,
            "reason_label": reason_meta(reason)[2],
            "batch_hint": reason_meta(reason)[3],
            "count": 0,
            "statuses": {},
        })
        group["count"] += 1
        status = record.get("recheck_status") or "isolated"
        group["statuses"][status] = group["statuses"].get(status, 0) + 1
    items = sorted(groups.values(), key=lambda item: (-item["count"], item["reason"]))
    assessment = _assess(items, len(records), total_files)
    return {
        "isolated_count": len(records),
        "categories": items,
        "assessment": assessment,
    }


def _assess(items: list[dict], total: int, total_files: int) -> dict:
    """给出批次级判断：规则整批不匹配，还是个别文件损坏。

    判断基于隔离文件内部的原因集中度，并结合任务总文件数给出规模参考。
    少量同因文件（如 80 个里 2 个编码损坏）按单文件处理；达到 3 个及以上
    且占任务文件数一定比例时提示整批问题。
    """
    if not items or not total:
        return {"scope": "none", "reason": "", "hint": "当前没有隔离文件"}
    dominant = items[0]
    group_share = dominant["count"] / total
    task_share = dominant["count"] / total_files if total_files else 0.0
    batch = group_share >= 0.5 and (
        dominant["count"] >= 3 or (total_files and total_files <= 4)
    )
    if not batch:
        scope = "file"
        hint = f"失败集中在个别文件（{dominant['count']} 个），可按单文件损坏优先复核"
    elif dominant["category"] == "rule":
        scope = "rule_set"
        hint = "同因规则匹配失败占隔离文件一半以上，优先检查规则 form_selector/marker 是否适配整批文件"
    elif dominant["category"] == "structure":
        scope = "batch_structure"
        hint = "同因结构异常占隔离文件一半以上，优先检查源文件导出/模板是否整体丢失结构"
    elif dominant["category"] == "encoding":
        scope = "batch_encoding"
        hint = "同因编码失败占隔离文件一半以上，优先检查源文件导出的整体编码设置"
    elif dominant["category"] == "processing":
        scope = "batch_processing"
        hint = "同因处理失败占隔离文件一半以上，优先检查并发、超时与运行环境"
    else:
        scope = "batch_scan"
        hint = "同因扫描失败占隔离文件一半以上，优先检查目录权限与磁盘状态"
    return {
        "scope": scope,
        "reason": dominant["reason"],
        "category": dominant["category"],
        "count": dominant["count"],
        "ratio": round(group_share, 4),
        "batch_share": round(task_share, 4),
        "hint": hint,
        "total_files": total_files,
    }
