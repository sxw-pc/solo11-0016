"""字段联动求值引擎：拓扑求值、失败降级与影响面重算。"""
from __future__ import annotations

from typing import Any, Optional

from archive_extractor.core.linkage.expressions import (
    ExpressionEvaluator,
    is_empty,
)
from archive_extractor.core.linkage.graph import (
    DEGRADED,
    FAILED,
    OK,
    DependencyGraph,
    LinkageError,
)
from archive_extractor.utils.common import utc_now

_CONSTRAINT_VALIDATOR = "linkage_constraint"
_DERIVATION_VALIDATOR = "linkage_derivation"


class LinkageEngine:
    """对单条规则持有依赖图，逐文件执行联动求值。"""

    def __init__(self, rule: dict, graph: DependencyGraph) -> None:
        self.rule = rule
        self.graph = graph
        self.evaluator = ExpressionEvaluator()

    # ------------------------------------------------------------------
    # 全量求值：抽取完成后调用，写回 data 并产出溯源与降级问题
    # ------------------------------------------------------------------
    def evaluate(self, extracted: dict) -> dict:
        data = extracted.setdefault("data", {})
        parse_errors = {item["field"] for item in extracted.get("errors", [])}
        states: dict[str, str] = {}
        traces: list[dict] = []
        issues: list[dict] = []
        for target in self.graph.order:
            trace, issue = self._evaluate_one(
                target, data, states, parse_errors,
                extracted.get("rule_version", ""),
            )
            traces.append(trace)
            if issue:
                issues.append(issue)
        constraint_issues, constraint_traces = self._check_constraints(
            data, states, parse_errors, extracted.get("rule_version", "")
        )
        issues.extend(constraint_issues)
        traces.extend(constraint_traces)
        extracted["linkage"] = {
            "states": states,
            "traces": traces,
            "issues": issues,
        }
        return extracted

    # ------------------------------------------------------------------
    # 局部重算：只有 changed_fields 的下游派生字段与相关约束会被重算
    # ------------------------------------------------------------------
    def rederive(
        self,
        data: dict,
        changed_fields: list[str],
        rule_version: str,
        previous_states: Optional[dict[str, str]] = None,
    ) -> dict:
        states = dict(previous_states or {})
        parse_errors: set[str] = set()
        targets: list[str] = []
        for field in changed_fields:
            for target in self.graph.affected_by(field):
                if target not in targets:
                    targets.append(target)
        traces: list[dict] = []
        issues: list[dict] = []
        for target in targets:
            trace, issue = self._evaluate_one(
                target, data, states, parse_errors, rule_version
            )
            traces.append(trace)
            if issue:
                issues.append(issue)
        touched: list[dict] = []
        for field in changed_fields:
            touched.extend(self.graph.constraints_touching(field))
        unique_constraints = {id(item): item for item in touched}.values()
        constraint_issues, constraint_traces = self._check_constraints(
            data, states, parse_errors, rule_version,
            only=list(unique_constraints),
        )
        issues.extend(constraint_issues)
        traces.extend(constraint_traces)
        return {
            "recomputed": targets,
            "states": states,
            "traces": traces,
            "issues": issues,
            "constraints_checked": [id(item) for item in unique_constraints],
        }

    # ------------------------------------------------------------------
    def _evaluate_one(
        self,
        target: str,
        data: dict,
        states: dict[str, str],
        parse_errors: set[str],
        rule_version: str,
    ) -> tuple[dict, Optional[dict]]:
        spec = self.graph.derivations[target]
        expr = spec["expression"]
        inputs = spec["_refs"]
        upstream_values = {name: data.get(name) for name in inputs}
        now = utc_now()

        def make_trace(status: str, value: Any, reason: str = "") -> dict:
            return {
                "field": target,
                "selector": "",
                "selector_type": "derivation",
                "source_line": 0,
                "raw_value": _summarize_inputs(upstream_values),
                "value": value,
                "extracted_at": now,
                "rule_version": rule_version,
                "source_md5": "",
                "derived": 1,
                "upstream_fields": ",".join(inputs),
                "expression": _expression_text(expr),
                "linkage_status": status,
                "linkage_reason": reason,
            }

        failed_input = next(
            (
                name for name in inputs
                if name in parse_errors
                or (name in states and states[name] == FAILED)
            ),
            None,
        )
        if failed_input is not None:
            states[target] = FAILED
            data[target] = None
            reason = f"上游字段 {failed_input} 抽取失败，按失败路径降级"
            return make_trace(FAILED, None, reason), self._degrade_issue(
                spec, target, reason
            )
        empty_input = next(
            (
                name for name in inputs
                if (name in states and states[name] == DEGRADED)
                or is_empty(data.get(name))
            ),
            None,
        )
        if empty_input is not None:
            states[target] = DEGRADED
            data[target] = None
            reason = f"上游字段 {empty_input} 为空，无法计算，按空值路径降级"
            return make_trace(DEGRADED, None, reason), self._degrade_issue(
                spec, target, reason
            )
        try:
            value = self.evaluator.evaluate(expr, data)
        except (LinkageError, TypeError, ValueError, AttributeError) as exc:
            states[target] = FAILED
            data[target] = None
            reason = f"表达式求值失败: {exc}"
            return make_trace(FAILED, None, reason), self._degrade_issue(
                spec, target, reason
            )
        states[target] = OK
        data[target] = value
        return make_trace(OK, value), None

    @staticmethod
    def _degrade_issue(spec: dict, field: str, reason: str) -> dict:
        return {
            "level": spec.get("on_failure", "warning"),
            "field": field,
            "message": reason,
            "validator": _DERIVATION_VALIDATOR,
        }

    # ------------------------------------------------------------------
    def _check_constraints(
        self,
        data: dict,
        states: dict[str, str],
        parse_errors: set[str],
        rule_version: str,
        only: Optional[list[dict]] = None,
    ) -> tuple[list[dict], list[dict]]:
        issues: list[dict] = []
        traces: list[dict] = []
        for item in only or self.graph.constraints:
            refs = item["_refs"]
            unavailable = any(
                name in parse_errors or states.get(name) in {FAILED, DEGRADED}
                or is_empty(data.get(name))
                for name in refs
            )
            if unavailable:
                # 输入不可用时跳过约束，避免拿空值误判，同时留痕
                traces.append(self._constraint_trace(item, data, "skipped",
                                                     rule_version))
                continue
            passed, message = self._evaluate_constraint(item, data)
            if passed:
                traces.append(self._constraint_trace(item, data, OK,
                                                     rule_version))
                continue
            issues.append({
                "level": item.get("level", "error"),
                "field": _constraint_field(item),
                "message": message or item.get(
                    "message", "关联字段逻辑校验未通过"
                ),
                "validator": _CONSTRAINT_VALIDATOR,
            })
            traces.append(
                self._constraint_trace(item, data, "violated", rule_version)
            )
        return issues, traces

    def _evaluate_constraint(self, item: dict, data: dict) -> tuple[bool, str]:
        kind = item["type"]
        if kind == "date_order":
            return self._date_order(item, data)
        return self._compare(item, data)

    def _date_order(self, item: dict, data: dict) -> tuple[bool, str]:
        from archive_extractor.core.validator import _parse_date

        start = _parse_date(data.get(item["start"]))
        end = _parse_date(data.get(item["end"]))
        if start is None or end is None:
            return True, ""
        if end < start:
            return False, item.get(
                "message",
                f"{item['end']} 不得早于 {item['start']}",
            )
        return True, ""

    def _compare(self, item: dict, data: dict) -> tuple[bool, str]:
        from archive_extractor.core.validator import _compare

        left = data.get(item["left"])
        right = data.get(item["right_field"]) if item.get(
            "right_field"
        ) else item.get("right")
        if _compare(left, str(item.get("op", "==")), right):
            return True, ""
        return False, item.get("message", "关联字段比较校验未通过")

    @staticmethod
    def _constraint_trace(item: dict, data: dict, status: str,
                          rule_version: str) -> dict:
        refs = item["_refs"]
        values = {name: data.get(name) for name in refs}
        return {
            "field": _constraint_field(item),
            "selector": "",
            "selector_type": "constraint",
            "source_line": 0,
            "raw_value": _summarize_inputs(values),
            "value": None,
            "extracted_at": utc_now(),
            "rule_version": rule_version,
            "source_md5": "",
            "derived": 0,
            "upstream_fields": ",".join(refs),
            "expression": f"{item['type']}:{item.get('op', '')}",
            "linkage_status": status,
            "linkage_reason": item.get("message", ""),
        }


def _constraint_field(item: dict) -> str:
    if item["type"] == "date_order":
        return f"{item['start']}~{item['end']}"
    return str(item.get("left", ""))


def _summarize_inputs(values: dict[str, Any]) -> str:
    return "; ".join(f"{name}={values[name]!r}" for name in values)[:500]


def _expression_text(expr: dict) -> str:
    kind = expr.get("type", "")
    if kind == "concat":
        return f"concat:{expr.get('separator', '')}"
    if kind == "case":
        return f"case:{expr.get('field', '')}"
    if kind == "slice":
        return f"slice:{expr.get('field', '')}[{expr.get('start', 0)}:{expr.get('end', '')}]"
    return kind
