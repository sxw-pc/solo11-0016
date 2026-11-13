"""规则试跑与差异比对子命令的核心包。

本包为独立增量模块：不修改既有解析、校验、去重与台账导出链路，
试跑数据只落在独立临时库/内存结构中，运行结束即清理。
"""
from archive_extractor.core.rule_diff.analyzer import analyze_rules
from archive_extractor.core.rule_diff.comparator import compare_ledgers
from archive_extractor.core.rule_diff.engine import (
    DryRunInterrupted,
    RuleDryRun,
    sweep_staging_dirs,
)
from archive_extractor.core.rule_diff.runner import (
    load_rule_source,
    run_rule_diff,
    write_report,
)

__all__ = [
    "analyze_rules",
    "compare_ledgers",
    "DryRunInterrupted",
    "RuleDryRun",
    "sweep_staging_dirs",
    "load_rule_source",
    "run_rule_diff",
    "write_report",
]
