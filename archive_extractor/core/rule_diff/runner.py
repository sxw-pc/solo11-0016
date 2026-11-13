"""规则试跑与差异比对的编排入口。

流程：加载两套规则（版本号生成方式与主链路完全一致）
→ 扫描同一目录并确认两侧文件清单与 MD5 相同
→ 各自在独立临时库中隔离试跑 → 字段级差异比对 → 组装结构化报告。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Union

from archive_extractor.config.loader import (
    RuleSet,
    load_rule_file,
    load_rules,
)
from archive_extractor.core.rule_diff.analyzer import analyze_rules
from archive_extractor.core.rule_diff.comparator import compare_ledgers
from archive_extractor.core.rule_diff.engine import (
    RuleDryRun,
    sweep_staging_dirs,
)
from archive_extractor.core.scanner import DirectoryScanner
from archive_extractor.utils.common import RuleError

PathLike = Union[str, Path]


def load_rule_source(source: PathLike) -> RuleSet:
    """加载规则目录或单个规则文件。

    单文件场景复用与目录加载相同的 ``load_rule_file`` 校验入口和
    ``RuleSet`` 版本签名算法，保证试跑版本号与正式任务完全一致，
    同时兼容历史规则文件（缺少可选字段时由 schema 补默认值）。
    """
    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise RuleError(f"规则路径不存在: {path}")
    if path.is_dir():
        return load_rules(path)
    return RuleSet([load_rule_file(path)], path.parent)


def _scan_manifest(scan_root: PathLike) -> dict[str, dict]:
    items = DirectoryScanner(scan_root).scan()
    return {
        item.rel_path: {
            "rel_path": item.rel_path,
            "md5": item.md5,
            "size": item.size,
        }
        for item in items
    }


def _assert_same_inputs(
    old_manifest: dict[str, dict], new_manifest: dict[str, dict]
) -> None:
    """两套规则面对的必须是同一批静态文件；MD5 不同则拒绝给出误导性结论。"""
    if old_manifest != new_manifest:
        old_only = sorted(set(old_manifest) - set(new_manifest))
        new_only = sorted(set(new_manifest) - set(old_manifest))
        md5_mismatch = sorted(
            path for path in set(old_manifest) & set(new_manifest)
            if old_manifest[path]["md5"] != new_manifest[path]["md5"]
        )
        raise RuleError(
            "两次扫描的输入文件清单不一致，无法进行规则差异比对："
            f"仅旧侧存在 {len(old_only)} 个、仅新侧存在 {len(new_only)} 个、"
            f"MD5 不一致 {len(md5_mismatch)} 个"
            + (f"，示例: {md5_mismatch[:3]}" if md5_mismatch else "")
        )


def run_rule_diff(
    scan_root: PathLike,
    old_rules: PathLike,
    new_rules: PathLike,
    *,
    timeout: float = 10,
    concurrency: int = 1,
    limit: Optional[int] = None,
    staging_base: Optional[PathLike] = None,
    clean_staging: bool = True,
) -> dict:
    """执行完整的双规则试跑与差异比对，返回可 JSON 序列化的报告。"""
    if clean_staging:
        swept = sweep_staging_dirs(
            Path(staging_base) if staging_base else None
        )
    else:
        swept = []
    scan_root = Path(scan_root).expanduser().resolve()
    old_ruleset = load_rule_source(old_rules)
    new_ruleset = load_rule_source(new_rules)

    old_manifest = _scan_manifest(scan_root)
    new_manifest = _scan_manifest(scan_root)
    _assert_same_inputs(old_manifest, new_manifest)

    analysis = analyze_rules(old_ruleset, new_ruleset)

    staging = Path(staging_base) if staging_base else None
    old_dry = RuleDryRun(
        old_ruleset, scan_root, timeout=timeout, concurrency=concurrency,
        limit=limit, staging_base=staging,
    )
    try:
        old_ledger = old_dry.execute()
        new_dry = RuleDryRun(
            new_ruleset, scan_root, timeout=timeout, concurrency=concurrency,
            limit=limit, staging_base=staging,
        )
        try:
            new_ledger = new_dry.execute()
        finally:
            new_dry.close()
    finally:
        old_dry.close()

    report = compare_ledgers(old_ledger, new_ledger, analysis)
    report["input"] = {
        "scan_root": str(scan_root),
        "old_rules": str(Path(old_rules).expanduser().resolve()),
        "new_rules": str(Path(new_rules).expanduser().resolve()),
        "scanned_files": [
            {
                "rel_path": rel_path,
                "md5": item["md5"],
                "size": item["size"],
            }
            for rel_path, item in sorted(old_manifest.items())
        ],
    }
    report["ledgers"] = {
        "old": _ledger_snapshot(old_ledger),
        "new": _ledger_snapshot(new_ledger),
    }
    report["staging"] = {"swept_leftovers": swept}
    return report


def _ledger_snapshot(ledger: dict) -> dict:
    """保留完整试跑台账（内存结构），用于同版本一致性验收。"""
    return {
        "task_status": ledger["task_status"],
        "ruleset_signature": ledger["ruleset_signature"],
        "rule_versions": ledger["rule_versions"],
        "records": ledger["records"],
    }


def write_report(report: dict, output: PathLike) -> Path:
    """把结构化报告写入 JSON 文件。"""
    target = Path(output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target
