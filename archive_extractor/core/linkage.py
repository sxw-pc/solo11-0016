"""字段联动层：派生字段求值、跨字段约束、依赖图与增量重算。

规则中通过 `fields.<name>.derived` 声明派生字段（A 由 B、C 拼接或条件
映射得到），通过 `linkage.constraints` 声明跨字段约束（X 晚于 Y 才合法）。
加载规则时构建依赖图并检测环，直接报错；抽取后按拓扑序求值；字段因
重跑、规则升级或源文件局部变化而更新时，仅重算受影响的派生字段与约束，
其余保持不动。上游字段提取失败时，派生字段按失败路径降级并记录台账。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from archive_extractor.core.validator import (
    ValidationIssue,
    _compare,
    _parse_date,
)
from archive_extractor.utils.common import RuleError, utc_now

DERIVED_OPS = {"concat", "template", "map", "regex_map", "coalesce"}
CONSTRAINT_TYPES = {"date_after", "date_before", "compare"}
COMPARE_OPS = {">", ">=", "<", "<=", "==", "!="}
TEMPLATE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.-]{0,63})\}")
DERIVED_VALIDATOR = "linkage:derived"
LEVELS = {"error", "warning", "info"}
_MAX_RAW = 500


class _MapMiss(Exception):
    """映射类表达式未命中任何分支且未配置 default。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuleError(message)


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _nested(data: Any, path: str) -> Any:
    value = data
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def spec_deps(spec: dict) -> list[str]:
    """返回一个派生表达式引用的全部上游字段路径。"""
    op = spec.get("op")
    if op in {"map", "regex_map"}:
        return [str(spec.get("source", ""))]
    if op == "template":
        return TEMPLATE_RE.findall(str(spec.get("template", "")))
    return [str(item) for item in spec.get("args", [])]


def _expr_text(spec: dict) -> str:
    op = spec.get("op")
    if op == "template":
        return f"template:{spec.get('template', '')}"
    if op in {"map", "regex_map"}:
        return f"{op}({spec.get('source', '')})"
    return f"{op}({','.join(spec.get('args', []))})"


def _constraint_name(item: dict, index: int) -> str:
    return str(item.get("name") or f"{item.get('type')}_{index}")


def _constraint_refs(item: dict) -> list[str]:
    refs = [item.get("field")]
    if item.get("type") in {"date_after", "date_before"}:
        refs.append(item.get("than"))
    elif item.get("right_field"):
        refs.append(item.get("right_field"))
    return [str(ref) for ref in refs if ref]


def _dep_hit(dep: str, changed: set) -> bool:
    if dep in changed or dep.split(".")[0] in changed:
        return True
    prefix = dep + "."
    return any(item.startswith(prefix) for item in changed)


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
        _require(isinstance(cases, dict) and bool(cases), f"派生字段 {name} 缺少 cases")
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
    name = _constraint_name(item, index)
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
    for ref in _constraint_refs(item):
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


def _topo_order(graph: dict) -> list[str]:
    indegree = {name: 0 for name in graph}
    for name, outs in graph.items():
        for nxt in outs:
            indegree[nxt] += 1
    queue = sorted(name for name, degree in indegree.items() if degree == 0)
    order = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for nxt in sorted(graph.get(node, ())):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(graph):
        raise RuleError("字段联动存在循环依赖")
    return order


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


class LinkageEngine:
    """单条规则的联动求值器；图结构已在加载期校验无环。"""

    def __init__(self, rule: dict) -> None:
        fields = rule.get("fields", {})
        self.labels = {
            name: field.get("label", name)
            for name, field in fields.items() if isinstance(field, dict)
        }
        self.derived = {
            name: field["derived"] for name, field in fields.items()
            if isinstance(field, dict) and "derived" in field
        }
        self.dep_map = {name: spec_deps(spec) for name, spec in self.derived.items()}
        graph = {
            name: {dep for dep in deps if dep in self.derived}
            for name, deps in self.dep_map.items()
        }
        self.order = _topo_order(graph)
        constraints = (rule.get("linkage") or {}).get("constraints", [])
        self.constraints = [
            (_constraint_name(item, index), item)
            for index, item in enumerate(constraints)
        ]
        self.empty = not self.derived and not self.constraints

    def affected(self, changed: set) -> set:
        """计算受变更字段影响的派生字段闭包（含间接下游）。"""
        hit = set(changed)
        affected: set = set()
        for name in self.order:
            if any(_dep_hit(dep, hit) for dep in self.dep_map[name]):
                affected.add(name)
                hit.add(name)
        return affected

    def evaluate(
        self,
        data: dict,
        failed: Optional[set] = None,
        changed: Optional[set] = None,
        carry: Optional[dict] = None,
        carry_issues: Optional[list] = None,
    ) -> dict:
        """按拓扑序求值派生字段并检查约束，结果就地写入 data。

        changed 为 None 时全量求值；否则仅重算受影响闭包，未受影响的
        派生字段沿用 carry 中的上一版本值，未受影响的约束沿用上一版本
        问题记录（carry_issues）。降级状态每次均按当前数据重新判定。
        """
        outcome = {"values": {}, "traces": [], "issues": [], "computed": [],
                   "carried": [], "degraded": [], "checked_constraints": [],
                   "carried_constraints": []}
        if self.empty:
            return outcome
        failed = set(failed or ())
        carry = carry if isinstance(carry, dict) else {}
        compute = set(self.derived) if changed is None else self.affected(changed)
        failed_derived: set = set()
        for name in self.order:
            spec = self.derived[name]
            deps = self.dep_map[name]
            fail_dep = next((dep for dep in deps if dep.split(".")[0] in failed
                             or dep in failed_derived), None)
            empty_dep = None if fail_dep else next(
                (dep for dep in deps if _empty(_nested(data, dep))), None)
            if fail_dep:
                reason = f"上游字段 {self.labels.get(fail_dep, fail_dep)} 提取失败"
                self._degrade(outcome, data, name, spec, "error", reason)
                failed_derived.add(name)
                continue
            if empty_dep and spec.get("op") != "coalesce":
                reason = f"上游字段 {self.labels.get(empty_dep, empty_dep)} 为空"
                self._degrade(outcome, data, name, spec, "info", reason)
                continue
            if name not in compute and carry.get(name) is not None:
                data[name] = carry[name]
                outcome["values"][name] = carry[name]
                outcome["carried"].append(name)
                outcome["traces"].append(self._trace(name, spec, data, carried=True))
                continue
            try:
                value = self._apply(spec, data)
            except _MapMiss as exc:
                self._degrade(outcome, data, name, spec, "warning", str(exc))
                continue
            data[name] = value
            outcome["values"][name] = value
            outcome["computed"].append(name)
            outcome["traces"].append(self._trace(name, spec, data))
        self._check_constraints(outcome, data, changed, compute, carry_issues or [])
        return outcome

    def _degrade(self, outcome, data, name, spec, level, reason) -> None:
        data[name] = None
        outcome["values"][name] = None
        outcome["degraded"].append(name)
        label = self.labels.get(name, name)
        outcome["issues"].append(ValidationIssue(
            level, name, f"派生字段{label}未计算：{reason}", DERIVED_VALIDATOR))
        outcome["traces"].append(self._trace(name, spec, data, degraded=reason))

    def _trace(self, name, spec, data, carried=False, degraded="") -> dict:
        raw = {"_upstream": {dep: _nested(data, dep)
                             for dep in self.dep_map[name]}}
        if carried:
            raw["_carried"] = True
        if degraded:
            raw["_degraded"] = degraded
        return {
            "field": name,
            "selector": _expr_text(spec),
            "selector_type": "derived",
            "source_line": 0,
            "raw_value": json.dumps(raw, ensure_ascii=False)[:_MAX_RAW],
            "value": data.get(name),
            "extracted_at": utc_now(),
        }

    def _apply(self, spec: dict, data: dict) -> Any:
        op = spec.get("op")
        if op == "concat":
            separator = str(spec.get("separator", ""))
            return separator.join(
                _stringify(_nested(data, dep)) for dep in spec["args"])
        if op == "template":
            return TEMPLATE_RE.sub(
                lambda match: _stringify(_nested(data, match.group(1))),
                str(spec["template"]),
            )
        if op == "coalesce":
            for dep in spec["args"]:
                value = _nested(data, dep)
                if not _empty(value):
                    return value
            return None
        key = _stringify(_nested(data, spec["source"]))
        if op == "map":
            cases = {str(k): v for k, v in spec.get("cases", {}).items()}
            if key in cases:
                return cases[key]
        else:
            for pattern, target in spec.get("cases", {}).items():
                if re.match(str(pattern), key):
                    return target
        if "default" in spec:
            return spec["default"]
        raise _MapMiss(f"值 {key!r} 未命中任何映射分支")

    def _check_constraints(self, outcome, data, changed, compute, carry_issues):
        scope = None if changed is None else set(changed) | compute
        for name, item in self.constraints:
            validator = f"linkage:{name}"
            if scope is not None and not any(
                    _dep_hit(ref, scope) for ref in _constraint_refs(item)):
                outcome["carried_constraints"].append(name)
                for old in carry_issues:
                    if old.get("validator") == validator:
                        outcome["issues"].append(ValidationIssue(
                            old["level"], old["field"], old["message"], validator))
                continue
            outcome["checked_constraints"].append(name)
            issue = self._eval_constraint(item, name, data)
            if issue:
                outcome["issues"].append(issue)

    def _eval_constraint(self, item, name, data) -> Optional[ValidationIssue]:
        kind = item.get("type")
        violated = False
        if kind in {"date_after", "date_before"}:
            left = _parse_date(_nested(data, item.get("field")))
            right = _parse_date(_nested(data, item.get("than")))
            if left is None or right is None:
                return None
            violated = left < right if kind == "date_after" else left > right
        else:
            right = item.get("right")
            if item.get("right_field"):
                right = _nested(data, item["right_field"])
            violated = not _compare(
                _nested(data, item.get("field")), str(item.get("op", "==")), right)
        if not violated:
            return None
        return ValidationIssue(
            item.get("level", "error"), item.get("field", ""),
            item.get("message") or f"联动约束 {name} 未通过", f"linkage:{name}")


def incremental_kwargs(rule: dict, data: dict, carry: Optional[dict]) -> dict:
    """根据上一版本结果计算增量求值参数；无历史或规则版本变化时全量求值。"""
    if not carry or carry.get("rule_version") != rule.get("_effective_version"):
        return {"changed": None}
    old = carry.get("data") or {}
    changed = {
        name for name, field in rule.get("fields", {}).items()
        if not (isinstance(field, dict) and "derived" in field)
        and data.get(name) != old.get(name)
    }
    return {"changed": changed, "carry": old,
            "carry_issues": carry.get("issues", [])}


def apply_linkage(engine: LinkageEngine, rule: dict, data: dict,
                  traces: list, errors: list, carry: Optional[dict]):
    """抽取完成后执行联动求值，合并派生溯源，返回 (问题列表, 元信息)。"""
    if engine.empty:
        return [], {}
    outcome = engine.evaluate(
        data,
        failed={item["field"] for item in errors},
        **incremental_kwargs(rule, data, carry),
    )
    traces.extend(outcome["traces"])
    meta = {key: outcome[key] for key in ("computed", "carried", "degraded")}
    return outcome["issues"], meta


def build_carry(db, task_id: int, rel_path: str) -> Optional[dict]:
    """读取同任务同文件的上一版本结果，作为增量重算的基线。"""
    row = db.query_one(
        """SELECT id, data_json, rule_version FROM results
           WHERE task_id = ? AND source_rel_path = ?
           ORDER BY version DESC LIMIT 1""",
        (task_id, rel_path),
    )
    if not row:
        return None
    issues = db.query(
        """SELECT level, field, message, validator FROM validations
           WHERE result_id = ? AND validator LIKE 'linkage:%'""",
        (row["id"],),
    )
    return {
        "rule_version": row.get("rule_version", ""),
        "data": json.loads(row.get("data_json") or "{}"),
        "issues": issues,
    }
