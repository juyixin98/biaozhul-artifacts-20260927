"""安全内核：对单个请求做三态判定。

判定顺序（不可由配置调换）：
  1. 任一 DENY 规则“确定命中”        -> DENY（显式拒绝优先）
  2. 任一 ALLOW 规则“确定命中”，
     且不存在“可能命中”的 DENY 规则   -> ALLOW
  3. 存在“可能命中”的规则（条件未知） -> UNKNOWN（绝不默认放行）
  4. 其余                              -> DENY（默认拒绝）

“可能命中”：前缀/操作/主体三个硬门通过，条件中没有确定为假、
但至少一个条件因属性未知/不可比而无法判定。

内核只依赖 parser 产出的不可变领域对象，不读取数据库、不做 I/O。
"""

from __future__ import annotations

import fnmatch
import ipaddress
from typing import Any

from .models import (
    Condition,
    ConditionOp,
    Decision,
    Effect,
    Policy,
    Request,
    Rule,
    TraceStep,
    UNKNOWN,
    Verdict,
)

_T = "TRUE"
_F = "FALSE"
_U = "UNKNOWN"


def _tri_and(values: list[str]) -> str:
    if _F in values:
        return _F
    if _U in values:
        return _U
    return _T


# ---------------------------------------------------------------------------
# 条件求值：返回 TRUE / FALSE / UNKNOWN
# ---------------------------------------------------------------------------
def eval_condition(cond: Condition, req: Request) -> str:
    v = req.attr(cond.attribute)
    op = cond.op

    if op is ConditionOp.EXISTS:
        return _T if v is not UNKNOWN else _F
    if op is ConditionOp.NOT_EXISTS:
        return _F if v is not UNKNOWN else _T

    # 其余算子在属性缺失时一律 UNKNOWN（包括 Not* —— 保守）
    if v is UNKNOWN:
        return _U

    if op is ConditionOp.EQ:
        # 属性存在但运行时类型与策略右值不可比 -> 无法判定（保守），
        # 而不是当作“不相等”。仅同类型时才做确定比较。
        if type(v) is not type(cond.value):
            return _U
        return _T if v == cond.value else _F
    if op is ConditionOp.NOT_EQ:
        if v is UNKNOWN:
            return _U
        if type(v) is not type(cond.value):
            return _U
        return _F if v == cond.value else _T
    if op is ConditionOp.IN:
        return _T if isinstance(v, str) and v in cond.value else _F
    if op is ConditionOp.NOT_IN:
        if not isinstance(v, str):
            return _U
        return _F if v in cond.value else _T

    if op in (
        ConditionOp.GREATER_THAN,
        ConditionOp.LESS_THAN,
        ConditionOp.GREATER_EQUAL,
        ConditionOp.LESS_EQUAL,
    ):
        return _eval_numeric(op, v, cond.value)

    if op in (ConditionOp.CIDR_MATCH, ConditionOp.NOT_CIDR_MATCH):
        inside = _eval_cidr(v, cond.value)
        if inside == _U:
            return _U
        return inside if op is ConditionOp.CIDR_MATCH else (_F if inside == _T else _T)

    if op in (ConditionOp.GLOB_MATCH, ConditionOp.NOT_GLOB_MATCH):
        if not isinstance(v, str):
            return _U
        matched = _T if fnmatch.fnmatchcase(v, cond.value) else _F
        return matched if op is ConditionOp.GLOB_MATCH else (
            _F if matched == _T else _T
        )

    # parser 已保证不会到这里；显式保守处理
    return _U  # pragma: no cover


def _scalar_equal(a: Any, b: Any) -> bool:
    # 严格类型："1" != 1，True != 1，1 != 1.0
    if type(a) is not type(b):
        return False
    return a == b


def _eval_numeric(op: ConditionOp, v: Any, threshold: Any) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return _U  # 运行时值不是数字 -> 无法判定，而不是假
    if op is ConditionOp.GREATER_THAN:
        return _T if v > threshold else _F
    if op is ConditionOp.LESS_THAN:
        return _T if v < threshold else _F
    if op is ConditionOp.GREATER_EQUAL:
        return _T if v >= threshold else _F
    return _T if v <= threshold else _F


def _eval_cidr(v: Any, cidr: str) -> str:
    if not isinstance(v, str):
        return _U
    try:
        addr = ipaddress.ip_address(v)
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return _U  # 畸形 IP / 网络 -> 无法判定
    return _T if addr in net else _F


# ---------------------------------------------------------------------------
# 规则级匹配：MATCH / NOT_MATCH / POSSIBLE_MATCH
# ---------------------------------------------------------------------------
MATCH = "MATCH"
NOT_MATCH = "NOT_MATCH"
POSSIBLE_MATCH = "POSSIBLE_MATCH"


def _principal_matches(rule: Rule, principal: str | None) -> bool:
    if principal is None:
        # 匿名请求只匹配显式开放匿名的规则（"*" 已在 parser 强制 anonymous=true）
        return rule.anonymous
    if "*" in rule.principals:
        return True
    return principal in rule.principals


def eval_rule(rule: Rule, req: Request) -> tuple[str, TraceStep]:
    prefix_m = req.resource.startswith(rule.resource_prefix)
    action_m = req.action in rule.actions
    principal_m = _principal_matches(rule, req.principal)

    cond_results: list[tuple[str, str, str]] = []
    cond_tri: list[str] = []
    for c in rule.conditions:
        r = eval_condition(c, req)
        cond_results.append((c.attribute, c.op.value, r))
        cond_tri.append(r)

    if not (prefix_m and action_m and principal_m):
        outcome = NOT_MATCH
    elif not rule.conditions:
        outcome = MATCH
    else:
        combined = _tri_and(cond_tri)
        outcome = MATCH if combined == _T else (
            NOT_MATCH if combined == _F else POSSIBLE_MATCH
        )

    step = TraceStep(
        step=0,  # 由 evaluate 编号
        rule_id=rule.id,
        effect=rule.effect.value,
        prefix_match=prefix_m,
        action_match=action_m,
        principal_match=principal_m,
        condition_results=tuple(cond_results),
        rule_outcome=outcome,
    )
    return outcome, step


def evaluate(policy: Policy, req: Request) -> Decision:
    deny_match_id: str | None = None
    allow_match_id: str | None = None
    deny_possible = False
    any_possible = False
    steps: list[TraceStep] = []

    for i, rule in enumerate(policy.rules, start=1):
        outcome, step = eval_rule(rule, req)
        steps.append(
            TraceStep(
                step=i,
                rule_id=step.rule_id,
                effect=step.effect,
                prefix_match=step.prefix_match,
                action_match=step.action_match,
                principal_match=step.principal_match,
                condition_results=step.condition_results,
                rule_outcome=step.rule_outcome,
            )
        )
        if outcome == MATCH:
            if rule.effect is Effect.DENY and deny_match_id is None:
                deny_match_id = rule.id
            if rule.effect is Effect.ALLOW and allow_match_id is None:
                allow_match_id = rule.id
        elif outcome == POSSIBLE_MATCH:
            any_possible = True
            if rule.effect is Effect.DENY:
                deny_possible = True

    if deny_match_id is not None:
        return Decision(
            verdict=Verdict.DENY,
            decided_by=deny_match_id,
            default_deny=False,
            trace=tuple(steps),
        )
    if allow_match_id is not None and not deny_possible:
        return Decision(
            verdict=Verdict.ALLOW,
            decided_by=allow_match_id,
            default_deny=False,
            trace=tuple(steps),
        )
    if any_possible or deny_possible:
        return Decision(
            verdict=Verdict.UNKNOWN,
            decided_by=None,
            default_deny=False,
            trace=tuple(steps),
        )
    return Decision(
        verdict=Verdict.DENY,
        decided_by=None,
        default_deny=True,
        trace=tuple(steps),
    )
