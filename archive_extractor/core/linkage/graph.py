"""联动依赖图：规则加载期构建、环检测与拓扑排序。

图中的节点是字段名（顶层字段）。边 ``upstream -> downstream`` 表示
``downstream`` 由 ``upstream`` 派生。跨字段约束不写入字段依赖图
（约束不产生值），只参与求值阶段的校验。
"""
from __future__ import annotations

from typing import Any, Iterable

from archive_extractor.utils.common import RuleError

# 联动求值状态（同时用于溯源索引中的 linkage_status）
OK = "ok"            # 正常求值
FAILED = "failed"    # 上游失败或表达式异常，按失败路径降级
DEGRADED = "degraded"  # 上游存在但为空，无法计算，按空值路径降级
MISSING = "missing"  # 引用的字段在数据中不存在（规则加载期已拦截，兜底用）


class LinkageError(RuleError):
    """联动规则结构非法或依赖图存在环。"""


EXPRESSION_TYPES = {
    "concat", "slice", "coalesce", "phone_kind", "case", "date_diff",
}


def _field_names(fields: dict, prefix: str = "") -> Iterable[str]:
    """枚举规则中声明的全部字段路径。"""
    for name, field in fields.items():
        path = f"{prefix}.{name}" if prefix else name
        yield path
        if field.get("type") == "object":
            yield from _field_names(field.get("fields", {}), path)


def _validate_reference(ref: Any, known: set[str], owner: str) -> str:
    if not isinstance(ref, str) or not ref:
        raise LinkageError(f"{owner} 的字段引用必须是非空字符串: {ref!r}")
    if "." in ref:
        raise LinkageError(f"{owner} 暂不支持对象内点号路径引用: {ref}")
    if ref not in known:
        raise LinkageError(f"{owner} 引用了未声明的字段: {ref}")
    return ref


def expression_references(expr: dict) -> list[str]:
    """按表达式类型枚举它引用的全部上游字段（保持声明顺序）。"""
    kind = expr.get("type")
    if kind == "concat":
        return [
            part["field"] for part in expr.get("parts", [])
            if isinstance(part, dict) and "field" in part
        ]
    if kind in {"slice", "phone_kind", "case"}:
        return [expr["field"]] if expr.get("field") else []
    if kind == "coalesce":
        return list(expr.get("inputs", []))
    if kind == "date_diff":
        return [name for name in (expr.get("start"), expr.get("end")) if name]
    raise LinkageError(f"不支持的表达式类型: {kind}")


def validate_expression(expr: dict, owner: str) -> None:
    """表达式结构在规则加载期完成校验，避免运行期才发现配置错误。"""
    kind = expr.get("type")
    if kind == "concat":
        parts = expr.get("parts")
        if not isinstance(parts, list) or not parts:
            raise LinkageError(f"{owner} concat 需要非空 parts 列表")
        for part in parts:
            if isinstance(part, str):
                continue
            if isinstance(part, dict) and "field" in part:
                continue
            raise LinkageError(f"{owner} concat 片段必须是字符串或 {{field: ...}}")
    elif kind == "slice":
        if not expr.get("field"):
            raise LinkageError(f"{owner} slice 缺少 field")
        if not isinstance(expr.get("start", 0), int) or (
            "end" in expr and not isinstance(expr["end"], int)
        ):
            raise LinkageError(f"{owner} slice 的 start/end 必须是整数")
    elif kind == "phone_kind":
        if not expr.get("field"):
            raise LinkageError(f"{owner} phone_kind 缺少 field")
    elif kind == "case":
        branches = expr.get("branches")
        if not isinstance(branches, list) or not branches:
            raise LinkageError(f"{owner} case 需要非空 branches 列表")
        for branch in branches:
            if not isinstance(branch, dict) or "value" not in branch or not (
                "equals" in branch or "pattern" in branch
            ):
                raise LinkageError(
                    f"{owner} case 分支需包含 equals/pattern 与 value"
                )
        if "default" not in expr:
            raise LinkageError(f"{owner} case 必须配置 default 兜底值")
    elif kind == "coalesce":
        if not expr.get("inputs"):
            raise LinkageError(f"{owner} coalesce 需要非空 inputs 列表")
    elif kind == "date_diff":
        if not expr.get("start") or not expr.get("end"):
            raise LinkageError(f"{owner} date_diff 缺少 start/end")


class DependencyGraph:
    """不可变的字段依赖图，提供拓扑序与影响面查询。"""

    def __init__(
        self,
        derivations: dict[str, dict],
        constraints: list[dict],
        order: list[str],
        upstream: dict[str, list[str]],
        downstream: dict[str, list[str]],
    ) -> None:
        self.derivations = derivations
        self.constraints = constraints
        self.order = order
        self.upstream = upstream
        self.downstream = downstream

    @property
    def derived_fields(self) -> list[str]:
        return list(self.order)

    def affected_by(self, field: str) -> list[str]:
        """返回字段变化后需要重算的全部派生字段（拓扑序，含自身若为派生）。

        非派生、无下游的字段返回空列表；结果保证按依赖顺序排列，
        可直接交给引擎按序重算。
        """
        affected: list[str] = []
        seen: set[str] = set()
        stack = list(self.downstream.get(field, ()))
        while stack:
            current = stack.pop(0)
            if current in seen:
                continue
            seen.add(current)
            affected.append(current)
            stack.extend(self.downstream.get(current, ()))
        rank = {name: index for index, name in enumerate(self.order)}
        return sorted(affected, key=lambda name: rank.get(name, 0))

    def constraints_touching(self, field: str) -> list[dict]:
        """返回引用了该字段的全部约束（含间接派生链上的约束）。"""
        watched = {field, *self.affected_by(field)}
        return [
            item for item in self.constraints
            if any(ref in watched for ref in item.get("_refs", ()))
        ]


def build_graph(rule: dict) -> DependencyGraph:
    """根据规则构建依赖图；结构问题与环在加载期直接报错。"""
    fields = rule.get("fields", {})
    known = set(_field_names(fields))
    derivations_raw = rule.get("derivations", []) or []
    constraints_raw = rule.get("constraints", []) or []
    if not isinstance(derivations_raw, list):
        raise LinkageError("derivations 必须是列表")
    if not isinstance(constraints_raw, list):
        raise LinkageError("constraints 必须是列表")

    derivations: dict[str, dict] = {}
    edges: dict[str, list[str]] = {}
    for index, item in enumerate(derivations_raw):
        owner = f"derivations[{index}]"
        if not isinstance(item, dict):
            raise LinkageError(f"{owner} 必须是对象")
        target = item.get("field")
        _validate_reference(target, known, owner)
        if target in derivations:
            raise LinkageError(f"派生字段重复声明: {target}")
        if "selector" in fields.get(target, {}) or "default" in fields.get(
            target, {}
        ):
            raise LinkageError(
                f"派生字段 {target} 不能同时配置 selector/default，"
                "派生值只能由联动层计算"
            )
        expr = item.get("expression")
        if not isinstance(expr, dict) or not expr:
            raise LinkageError(f"{owner}({target}) 缺少 expression 对象")
        kind = expr.get("type")
        if not kind:
            raise LinkageError(f"{owner}({target}) expression 缺少 type")
        if kind not in EXPRESSION_TYPES:
            raise LinkageError(
                f"{owner}({target}) 不支持的表达式类型: {kind}"
            )
        validate_expression(expr, f"{owner}({target})")
        refs = [
            _validate_reference(ref, known, f"{owner}({target})")
            for ref in expression_references(expr)
        ]
        if len(refs) != len(set(refs)):
            raise LinkageError(f"{owner}({target}) 的输入存在重复字段引用")
        normalized = dict(item)
        normalized["_refs"] = refs
        normalized.setdefault("on_failure", "warning")
        if normalized["on_failure"] not in {"error", "warning", "info"}:
            raise LinkageError(f"{owner}({target}) on_failure 等级非法")
        derivations[target] = normalized
        edges[target] = refs

    constraints: list[dict] = []
    for index, item in enumerate(constraints_raw):
        owner = f"constraints[{index}]"
        if not isinstance(item, dict):
            raise LinkageError(f"{owner} 必须是对象")
        kind = item.get("type")
        if kind not in {"date_order", "compare"}:
            raise LinkageError(f"{owner} 约束类型不支持: {kind}")
        refs: list[str] = []
        if kind == "date_order":
            refs.append(_validate_reference(item.get("start"), known, owner))
            refs.append(_validate_reference(item.get("end"), known, owner))
        else:
            refs.append(_validate_reference(item.get("left"), known, owner))
            right_field = item.get("right_field")
            if right_field:
                refs.append(
                    _validate_reference(right_field, known, owner)
                )
            if "right" not in item and not right_field:
                raise LinkageError(f"{owner} compare 必须配置 right 或 right_field")
        level = item.get("level", "error")
        if level not in {"error", "warning", "info"}:
            raise LinkageError(f"{owner} 约束等级非法")
        normalized = dict(item)
        normalized["_refs"] = refs
        normalized.setdefault("level", "error")
        constraints.append(normalized)

    order = _topological_sort(derivations, edges)
    upstream = {name: list(edges[name]) for name in order}
    downstream: dict[str, list[str]] = {}
    for target, refs in upstream.items():
        for ref in refs:
            downstream.setdefault(ref, []).append(target)
    return DependencyGraph(
        derivations, constraints, order, upstream, downstream
    )


def _topological_sort(
    derivations: dict[str, dict], edges: dict[str, list[str]]
) -> list[str]:
    """Kahn 拓扑排序；发现环时抛出包含环路径的加载期错误。"""
    indegree = {name: 0 for name in derivations}
    adjacency: dict[str, list[str]] = {}
    for target, refs in edges.items():
        for ref in refs:
            if ref in derivations:  # 非派生字段不参与排序
                adjacency.setdefault(ref, []).append(target)
                indegree[target] += 1
    queue = sorted(name for name, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while queue:
        current = queue.pop(0)
        order.append(current)
        for nxt in adjacency.get(current, []):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
        queue.sort()
    if len(order) != len(derivations):
        cycle = _find_cycle(derivations, edges)
        raise LinkageError(f"联动依赖存在环，规则加载被拒绝: {' -> '.join(cycle)}")
    return order


def _find_cycle(
    derivations: dict[str, dict], edges: dict[str, list[str]]
) -> list[str]:
    """在只剩环的子图里用 DFS 找一条具体环路径，用于错误提示。"""
    state: dict[str, int] = {}  # 0 未访问 1 在栈中 2 完成
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for ref in edges.get(node, []):
            if ref not in derivations:
                continue
            if state.get(ref, 0) == 1:
                start = stack.index(ref)
                return stack[start:] + [ref]
            if state.get(ref, 0) == 0:
                found = visit(ref)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for name in derivations:
        if state.get(name, 0) == 0:
            found = visit(name)
            if found:
                return found
    return list(derivations)
