"""字段联动层：派生关系、跨字段约束与依赖图驱动的增量求值。"""
from archive_extractor.core.linkage.engine import LinkageEngine
from archive_extractor.core.linkage.graph import (
    DEGRADED,
    FAILED,
    MISSING,
    OK,
    LinkageError,
    DependencyGraph,
    build_graph,
)

__all__ = [
    "LinkageEngine",
    "DependencyGraph",
    "build_graph",
    "LinkageError",
    "OK",
    "FAILED",
    "DEGRADED",
    "MISSING",
]
