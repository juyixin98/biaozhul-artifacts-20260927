"""内核三态判定测试：显式拒绝优先、默认拒绝、未知条件 UNKNOWN。

所有期望均在测试中独立写出（不调用被测内核生成参考答案）。
"""

from __future__ import annotations

from diffanalyzer.kernel import evaluate
from diffanalyzer.models import (
    Condition,
    ConditionOp,
    Effect,
    Policy,
    Request,
    Rule,
    Verdict,
)


def rule(rid, effect, prefix="", actions=("read",), principals=frozenset({"*"}),
         anonymous=True, conditions=()):
    return Rule(id=rid, effect=effect, resource_prefix=prefix,
                actions=frozenset(actions), principals=principals,
                anonymous=anonymous, conditions=tuple(conditions))


def pol(*rules, version="v"):
    return Policy(version=version, rules=tuple(rules), source_hash="x")


def req(principal=None, action="read", resource="", **attrs):
    return Request.make(principal, action, resource, attrs or None)


# ---------------------------------------------------------------------------
# 默认拒绝
# ---------------------------------------------------------------------------
def test_empty_policy_default_denies():
    d = evaluate(pol(), req())
    assert d.verdict is Verdict.DENY
    assert d.default_deny is True
    assert d.decided_by is None


def test_prefix_miss_default_denies():
    p = pol(rule("a", Effect.ALLOW, prefix="logs/"))
    d = evaluate(p, req(resource="logs-secret/x"))
    assert d.verdict is Verdict.DENY
    assert d.default_deny is True
    assert d.trace[0].rule_outcome == "NOT_MATCH"
    assert d.trace[0].prefix_match is False


def test_action_and_principal_gates():
    p = pol(rule("a", Effect.ALLOW, principals=frozenset({"acct/alice"}),
                 anonymous=False))
    assert evaluate(p, req(principal="acct/bob")).verdict is Verdict.DENY
    assert evaluate(p, req(principal=None)).verdict is Verdict.DENY
    assert evaluate(p, req(principal="acct/alice", action="write")).verdict \
        is Verdict.DENY
    assert evaluate(p, req(principal="acct/alice")).verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# 显式拒绝优先
# ---------------------------------------------------------------------------
def test_explicit_deny_beats_allow_even_when_listed_first_or_second():
    allow = rule("allow", Effect.ALLOW)
    deny = rule("deny", Effect.DENY,
                conditions=(Condition("ip", ConditionOp.CIDR_MATCH,
                                      "10.0.0.0/8"),))
    for ordered in ((allow, deny), (deny, allow)):
        p = pol(*ordered)
        d = evaluate(p, req(ip="10.0.0.9"))
        assert d.verdict is Verdict.DENY
        assert d.decided_by == "deny"
        assert d.default_deny is False


def test_deny_takes_effect_only_when_it_definitely_matches():
    # DENY 规则条件未知时不能压过确定的 ALLOW -> 必须 UNKNOWN 而非 ALLOW
    deny = rule("deny", Effect.DENY,
                conditions=(Condition("ip", ConditionOp.CIDR_MATCH,
                                      "10.0.0.0/8"),))
    allow = rule("allow", Effect.ALLOW)
    d = evaluate(pol(deny, allow), req())  # 无 ip 属性
    assert d.verdict is Verdict.UNKNOWN
    assert d.decided_by is None


# ---------------------------------------------------------------------------
# 未知条件 -> UNKNOWN（绝不默认允许）
# ---------------------------------------------------------------------------
def test_missing_attribute_on_eq_is_unknown_not_deny_or_allow():
    p = pol(rule("a", Effect.ALLOW,
                 conditions=(Condition("tls", ConditionOp.EQ, True),)))
    d = evaluate(p, req())
    assert d.verdict is Verdict.UNKNOWN


def test_negated_condition_unknown_is_still_unknown():
    # 这是“边界输入悄悄算错”的高发点：NotEq/NotIn 在属性缺失时不得当作 TRUE
    for op in (ConditionOp.NOT_EQ, ConditionOp.NOT_IN,
               ConditionOp.NOT_CIDR_MATCH, ConditionOp.NOT_GLOB_MATCH):
        value = {
            ConditionOp.NOT_EQ: True,
            ConditionOp.NOT_IN: ["x"],
            ConditionOp.NOT_CIDR_MATCH: "10.0.0.0/8",
            ConditionOp.NOT_GLOB_MATCH: "x*",
        }[op]
        p = pol(rule("a", Effect.ALLOW,
                     conditions=(Condition("a", op, value),)))
        assert evaluate(p, req()).verdict is Verdict.UNKNOWN, op.value


def test_numeric_and_cidr_truth_tables():
    p = pol(rule("a", Effect.ALLOW,
                 conditions=(Condition("port", ConditionOp.GREATER_THAN, 1024),)))
    assert evaluate(p, req(port=1025)).verdict is Verdict.ALLOW
    assert evaluate(p, req(port=1024)).verdict is Verdict.DENY  # 默认拒绝
    assert evaluate(p, req(port="high")).verdict is Verdict.UNKNOWN

    p2 = pol(rule("a", Effect.ALLOW,
                  conditions=(Condition("ip", ConditionOp.CIDR_MATCH,
                                         "10.0.0.0/8"),)))
    assert evaluate(p2, req(ip="10.255.255.255")).verdict is Verdict.ALLOW
    assert evaluate(p2, req(ip="11.0.0.1")).verdict is Verdict.DENY
    assert evaluate(p2, req(ip="garbage")).verdict is Verdict.UNKNOWN


def test_not_eq_concrete_value_is_deterministic():
    p = pol(rule("a", Effect.ALLOW,
                 conditions=(Condition("locked", ConditionOp.NOT_EQ, True),)))
    assert evaluate(p, req(locked=False)).verdict is Verdict.ALLOW
    assert evaluate(p, req(locked=True)).verdict is Verdict.DENY
    assert evaluate(p, req()).verdict is Verdict.UNKNOWN


def test_trace_records_each_rule_and_condition_tri_state():
    p = pol(
        rule("deny", Effect.DENY,
             conditions=(Condition("ip", ConditionOp.CIDR_MATCH,
                                   "10.0.0.0/8"),)),
        rule("allow", Effect.ALLOW,
             conditions=(Condition("tls", ConditionOp.EQ, True),)),
    )
    d = evaluate(p, req(ip="10.0.0.1"))
    assert [s.step for s in d.trace] == [1, 2]
    assert d.trace[0].rule_id == "deny"
    assert d.trace[0].condition_results[0][2] == "TRUE"
    assert d.trace[1].condition_results[0][2] == "UNKNOWN"
