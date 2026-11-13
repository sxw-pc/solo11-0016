"""字段联动的加载期校验：派生表达式、字段引用、约束与循环依赖。

规则中通过 `fields.<name>.derived` 声明派生字段（A 由 B、C 拼接或条件
映射得到），通过 `linkage.constraints` 声明跨字段约束（X 晚于 Y 才合法）。
本模块在规则加载时构建依赖图并检测环，直接报错，不等到运行期。
"""
from __future__ import annotations

import re
from typing import Any

from archive_extractor.utils.common import RuleError

DERIVED_OPS = {"concat", "template", "map", "regex_map", "coalesce"}
CONSTRAINT_TYPES = {"date_after", "date_before", "compare"}
COMPARE_OPS = {">", ">=", "<", "<=", "==", "!="}
TEMPLATE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.-]{0,63})\}")
DERIVED_VALIDATOR = "linkage:derived"
LEVELS = {"error", "warning", "info"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuleError(message)


def spec_deps(spec: dict) -> list[str]:
    """返回一个派生表达式引用的全部上游字段路径。"""
    op = spec.get("op")
    if op in {"map", "regex_map"}:
        return [str(spec.get("source", ""))]
    if op == "template":
        return TEMPLATE_RE.findall(str(spec.get("template", "")))
    return [str(item) for item in spec.get("args", [])]


def constraint_name(item: dict, index: int) -> str:
    """约束名称：可显式配置 name，否则按类型与序号生成稳定名称。"""
    return str(item.get("name") or f"{item.get('type')}_{index}")


def constraint_refs(item: dict) -> list[str]:
    """返回约束引用的字段路径，用于增量重算的影响面判断。"""
    refs = [item.get("field")]
    if item.get("type") in {"date_after", "date_before"}:
        refs.append(item.get("than"))
    elif item.get("right_field"):
        refs.append(item.get("right_field"))
    return [str(ref) for ref in refs if ref]


def _validate_ref(ref: Any, fields: dict, owner: str) -> None:
    _require(isinstance(ref, str) and bool(ref), f"{owner} 引用了非法字段名: {ref}")
    root, _, child = ref.partition(".")
    _require(root in fields, f"{owner} 引用了不存在的字段: {ref}")
    if child:
        target = fields[root]
        _require(
            isinstance(target, dict) and target.get("type") == "object"
            and child in target.get("fields", {}),
            f"{owner} 引用了不存在的子字段: {ref}",
        )


def _validate_spec(name: str, spec: Any, fields: dict) -> None:
    _require(isinstance(spec, dict), f"派生字段 {name} 的 derived 必须是对象")
    op = spec.get("op")
    _require(op in DERIVED_OPS, f"派生字段 {name} 不支持的操作: {op}")
    if op in {"concat", "coalesce"}:
        args = spec.get("args")
        _require(isinstance(args, list) and bool(args), f"派生字段 {name} 缺少 args")
    if op == "template":
        template = spec.get("template")
        _require(
            isinstance(template, str) and bool(TEMPLATE_RE.search(template)),
            f"派生字段 {name} 的 template 需包含 {{字段}} 占位符",
        )
    if op in {"map", "regex_map"}:
        _require(bool(spec.get("source")), f"派生字段 {name} 缺少 source")
        cases = spec.get("cases")
        _require(isinstance(cases, dict) and bool(cases),
                 f"派生字段 {name} 缺少 cases")
        if op == "regex_map":
            for pattern in cases:
                try:
                    re.compile(str(pattern))
                except re.error as exc:
                    raise RuleError(f"派生字段 {name} 正则错误: {exc}") from exc
    for dep in spec_deps(spec):
        _validate_ref(dep, fields, f"派生字段 {name}")


def _validate_constraint(item: Any, index: int, fields: dict, names: set) -> None:
    _require(isinstance(item, dict), f"linkage.constraints 第 {index} 项必须是对象")
    kind = item.get("type")
    _require(kind in CONSTRAINT_TYPES, f"联动约束类型不支持: {kind}")
    _require(item.get("level", "error") in LEVELS, "联动约束等级非法")
    name = constraint_name(item, index)
    _require(name not in names, f"联动约束名称重复: {name}")
    names.add(name)
    if kind in {"date_after", "date_before"}:
        _require(bool(item.get("field")) and bool(item.get("than")),
                 f"联动约束 {name} 缺少 field 或 than")
    else:
        _require(bool(item.get("field")), f"联动约束 {name} 缺少 field")
        _require(str(item.get("op")) in COMPARE_OPS,
                 f"联动约束 {name} 的比较符不支持: {item.get('op')}")
        _require("right" in item or "right_field" in item,
                 f"联动约束 {name} 缺少 right 或 right_field")
    for ref in constraint_refs(item):
        _validate_ref(ref, fields, f"联动约束 {name}")


def _detect_cycle(graph: dict) -> None:
    """对派生字段依赖图做深度优先环检测，报错时给出完整环路径。"""
    color = {name: 0 for name in graph}
    stack: list[str] = []

    def visit(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if color.get(nxt) == 1:
                cycle = stack[stack.index(nxt):] + [nxt]
                raise RuleError(f"字段联动存在循环依赖: {' -> '.join(cycle)}")
            if color.get(nxt) == 0:
                visit(nxt)
        stack.pop()
        color[node] = 2

    for node in sorted(graph):
        if color[node] == 0:
            visit(node)


def validate_linkage(rule: dict) -> None:
    """加载期校验：派生表达式、字段引用、约束配置与循环依赖。"""
    fields = rule.get("fields", {})
    linkage = rule.get("linkage", {})
    _require(isinstance(linkage, dict), "linkage 必须是对象")
    constraints = linkage.get("constraints", [])
    _require(isinstance(constraints, list), "linkage.constraints 必须是列表")
    derived = {
        name: field["derived"] for name, field in fields.items()
        if isinstance(field, dict) and "derived" in field
    }
    for name, spec in derived.items():
        _validate_spec(name, spec, fields)
    graph = {
        name: {dep for dep in spec_deps(spec) if dep in derived}
        for name, spec in derived.items()
    }
    _detect_cycle(graph)
    names: set = set()
    for index, item in enumerate(constraints):
        _validate_constraint(item, index, fields, names)
