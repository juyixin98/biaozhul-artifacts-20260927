"""差分引擎测试。

参考答案独立性：
- 期望的请求见证由测试用具体字面量手工给出（principal/action/resource/属性），
  再分别请求内核对*原始见证*在两版重放判定；
- test_exhaustive_transition_table_against_independent_oracle 使用一个与
  diffengine/kernel 完全无关的独立参考判定 reference_decide（直接按三态语义
  重写），逐点核对受限空间内的整张 old/new 转移表。
策略文档刻意不写兜底 deny-all：默认拒绝由内核提供，
否则“显式拒绝优先”会令所有 ALLOW 失效。
"""

from __future__ import annotations

import ipaddress

from diffanalyzer.diffengine import compute_diff
from diffanalyzer.kernel import evaluate
from diffanalyzer.models import Verdict
from diffanalyzer.parser import parse_policy
from diffanalyzer.universe import MISSING, build_universe
from diffanalyzer.models import Request


def _submit(service, sign_policy, doc):
    rid = service.auditor.new_request_id()
    return service.submit_policy(sign_policy(doc), request_id=rid, actor="tester")


def _diff(service, old, new, *, prefixes, actions, evidence=None):
    rid = service.auditor.new_request_id()
    return compute_diff(
        old_policy=old, new_policy=new,
        scope_prefixes=tuple(prefixes),
        scope_actions=frozenset(actions),
        resource_alphabet=service.cfg.resource_alphabet,
        configured_principals=service.cfg.principals,
        include_anonymous=True,
        max_space_size=service.cfg.max_space_size,
        witness_limit=service.cfg.witness_limit_per_bucket,
        auditor=service.auditor, request_id=rid, actor="tester",
        evidence_bundle=evidence,
    )


# ---------------------------------------------------------------------------
# 独立参考判定：不用 kernel/diffengine，直接按“显式拒绝优先/默认拒绝/未知”重写
# ---------------------------------------------------------------------------
def _ref_condition(cond, attrs):
    """返回 TRUE / FALSE / UNKNOWN。"""
    name, op, value = cond["attribute"], cond["op"], cond.get("value")
    missing = name not in attrs
    if op == "Exists":
        return "FALSE" if missing else "TRUE"
    if op == "NotExists":
        return "TRUE" if missing else "FALSE"
    if missing:
        return "UNKNOWN"
    got = attrs[name]
    if op == "Eq":
        return "TRUE" if type(got) is type(value) and got == value else "FALSE"
    if op == "NotEq":
        if type(got) is not type(value):
            return "UNKNOWN"
        return "FALSE" if got == value else "TRUE"
    if op in ("Gt", "Lt", "Gte", "Lte"):
        if isinstance(got, bool) or not isinstance(got, (int, float)):
            return "UNKNOWN"
        return {
            "Gt": got > value, "Lt": got < value,
            "Gte": got >= value, "Lte": got <= value,
        }[op] and "TRUE" or "FALSE"
    if op in ("CidrMatch", "NotCidrMatch"):
        try:
            inside = ipaddress.ip_address(got) in ipaddress.ip_network(
                value, strict=False)
        except ValueError:
            return "UNKNOWN"
        m = "TRUE" if inside else "FALSE"
        return m if op == "CidrMatch" else (
            "FALSE" if m == "TRUE" else "TRUE")
    if op in ("In", "NotIn"):
        if not isinstance(got, str):
            return "UNKNOWN"
        m = got in value
        return "TRUE" if m else "FALSE" if op == "In" else (
            "FALSE" if m else "TRUE")
    raise AssertionError("参考实现未覆盖算子: " + op)


def reference_decide(policy_doc, request):
    principal, action, resource, attrs = (
        request["principal"], request["action"],
        request["resource"], request["attributes"])
    deny_match = allow_match = False
    any_possible = deny_possible = False

    for r in policy_doc["rules"]:
        if not resource.startswith(r["resource_prefix"]):
            continue
        if action not in r["actions"]:
            continue
        if principal is None:
            if not r.get("anonymous", False):
                continue
        elif "*" not in r["principals"] and principal not in r["principals"]:
            continue

        tri = "TRUE"
        for cond in r.get("conditions", []):
            cr = _ref_condition(cond, attrs)
            if cr == "FALSE":
                tri = "FALSE"
                break
            if cr == "UNKNOWN":
                tri = "UNKNOWN"
        if tri == "TRUE":
            if r["effect"] == "DENY":
                deny_match = True
            else:
                allow_match = True
        elif tri == "UNKNOWN":
            any_possible = True
            if r["effect"] == "DENY":
                deny_possible = True

    if deny_match:
        return "DENY"
    if allow_match and not deny_possible:
        return "ALLOW"
    if any_possible:
        return "UNKNOWN"
    return "DENY"  # 默认拒绝


# ---------------------------------------------------------------------------
# 场景 1：重叠前缀边界——在 logs/ 上的加宽不得波及 logs-secret/，反之亦然
# ---------------------------------------------------------------------------
def test_overlapping_prefix_widening_is_localized_with_concrete_witness(
        service, sign_policy):
    old_doc = {"version": "v1", "rules": [
        {"id": "read", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["get"], "principals": ["acct/alice"]}]}
    new_doc = {"version": "v2", "rules": [
        {"id": "read", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["get"], "principals": ["acct/alice"]},
        {"id": "read-secret", "effect": "ALLOW",
         "resource_prefix": "logs-secret/", "actions": ["get"],
         "principals": ["acct/alice"]}]}
    old = _submit(service, sign_policy, old_doc)
    new = _submit(service, sign_policy, new_doc)

    res = _diff(service, old, new, prefixes=["logs/", "logs-secret/"],
                actions=["get"])
    assert res.summary["verdict"] == "WIDENED"
    assert res.summary["widened_requests"] > 0

    # 手工具体见证：logs-secret/0 下 alice 的 get（旧拒新允）
    concrete = Request.make("acct/alice", "get", "logs-secret/0")
    assert evaluate(old, concrete).verdict is Verdict.DENY
    assert evaluate(new, concrete).verdict is Verdict.ALLOW
    assert evaluate(old, concrete).default_deny is True

    # 引擎产出的每个见证都必须在两版真实复演成立（防止见证自生成错误）
    for w in res.witnesses["widened"]:
        r = w["request"]
        req = Request.make(r["principal"], r["action"], r["resource"],
                           r["attributes"])
        assert evaluate(old, req).verdict.value == "DENY" == w["old"]["verdict"]
        assert evaluate(new, req).verdict.value == "ALLOW" == w["new"]["verdict"]
        # 边界关键断言：新增允许只发生在 logs-secret/，不得越过目录边界
        assert r["resource"].startswith("logs-secret/")

    # logs/ 区域在两版判定一致，不得误报
    stable = Request.make("acct/alice", "get", "logs/0")
    assert evaluate(old, stable).verdict is Verdict.ALLOW
    assert evaluate(new, stable).verdict is Verdict.ALLOW
    # 其它主体两处都仍是默认拒绝
    other = Request.make("acct/bob", "get", "logs-secret/0")
    assert evaluate(old, other).verdict is Verdict.DENY
    assert evaluate(new, other).verdict is Verdict.DENY


# ---------------------------------------------------------------------------
# 场景 2：否定条件 + 未知值 => UNKNOWN（new_unknown），确定值才给确定结论
# ---------------------------------------------------------------------------
def test_negated_condition_with_unknown_value_is_unsure(service, sign_policy):
    old_doc = {"version": "v1", "rules": []}
    new_doc = {"version": "v2", "rules": [
        {"id": "put", "effect": "ALLOW", "resource_prefix": "tmp/",
         "actions": ["put"], "principals": ["*"], "anonymous": True,
         "conditions": [
             {"attribute": "locked", "op": "NotEq", "value": True}]}]}
    old = _submit(service, sign_policy, old_doc)
    new = _submit(service, sign_policy, new_doc)
    res = _diff(service, old, new, prefixes=["tmp/"], actions=["put"])

    # 同时存在确定加宽与未知点：结论必须显式带 UNKNOWN，不得是纯 WIDENED
    assert res.summary["verdict"] == "WIDENED_WITH_UNKNOWN"
    assert res.summary["has_uncertain_points"] is True
    assert res.summary["widened_requests"] > 0
    assert res.summary["new_unknown_requests"] > 0
    assert res.summary["unknown_points_total"] > 0

    missing = Request.make(None, "put", "tmp/0")
    assert evaluate(old, missing).verdict is Verdict.DENY
    assert evaluate(new, missing).verdict is Verdict.UNKNOWN

    unlocked = Request.make(None, "put", "tmp/0", {"locked": False})
    assert evaluate(old, unlocked).verdict is Verdict.DENY
    assert evaluate(new, unlocked).verdict is Verdict.ALLOW

    locked = Request.make(None, "put", "tmp/0", {"locked": True})
    assert evaluate(new, locked).verdict is Verdict.DENY  # 默认拒绝

    for w in res.witnesses["new_unknown"]:
        r = w["request"]
        req = Request.make(r["principal"], r["action"], r["resource"],
                           r["attributes"])
        assert evaluate(new, req).verdict is Verdict.UNKNOWN
        assert w["old"]["verdict"] == "DENY"

    # 不确定结论必须在审计中单列
    assert any(e["status"] == "INCONCLUSIVE"
               for e in service.store.query_audit(limit=200))


# ---------------------------------------------------------------------------
# 场景 3：无关规则变化（分析范围外）=> EQUIVALENT，零见证
# ---------------------------------------------------------------------------
def test_irrelevant_rule_change_outside_scope_is_equivalent(service, sign_policy):
    common = {"id": "read", "effect": "ALLOW", "resource_prefix": "logs/",
              "actions": ["get"], "principals": ["acct/alice"]}
    old_doc = {"version": "v1", "rules": [
        common,
        {"id": "old-metrics", "effect": "ALLOW", "resource_prefix": "metrics/",
         "actions": ["get"], "principals": ["acct/alice"]}]}
    new_doc = {"version": "v2", "rules": [
        common,
        {"id": "new-metrics", "effect": "DENY", "resource_prefix": "metrics/",
         "actions": ["get"], "principals": ["acct/alice"]}]}
    old = _submit(service, sign_policy, old_doc)
    new = _submit(service, sign_policy, new_doc)
    res = _diff(service, old, new, prefixes=["logs/"], actions=["get"])
    assert res.summary["verdict"] == "EQUIVALENT"
    assert res.summary["widened_requests"] == 0
    assert res.summary["removed_allow_requests"] == 0
    assert all(len(v) == 0 for v in res.witnesses.values())
    # 穷举确实发生了（不是跳过）
    assert res.summary["enumerated"] == res.summary["space_size"]


# ---------------------------------------------------------------------------
# 场景 4：收窄——新增拒绝的具体见证
# ---------------------------------------------------------------------------
def test_shrinking_emits_concrete_removed_allow_witness(service, sign_policy):
    old_doc = {"version": "v1", "rules": [
        {"id": "read", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["get"], "principals": ["*"], "anonymous": True}]}
    new_doc = {"version": "v2", "rules": [
        {"id": "read", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["get"], "principals": ["acct/alice"]}]}
    old = _submit(service, sign_policy, old_doc)
    new = _submit(service, sign_policy, new_doc)
    res = _diff(service, old, new, prefixes=["logs/"], actions=["get"])
    assert res.summary["verdict"] == "SHRUNK"
    assert res.summary["removed_allow_requests"] > 0

    concrete = Request.make("acct/bob", "get", "logs/0")
    assert evaluate(old, concrete).verdict is Verdict.ALLOW
    assert evaluate(new, concrete).verdict is Verdict.DENY
    assert any(w["request"]["principal"] == "acct/bob"
               and w["request"]["resource"] == "logs/0"
               for w in res.witnesses["removed_allow"])
    # 见证复演
    for w in res.witnesses["removed_allow"]:
        r = w["request"]
        req = Request.make(r["principal"], r["action"], r["resource"],
                           r["attributes"])
        assert evaluate(old, req).verdict is Verdict.ALLOW
        assert evaluate(new, req).verdict is Verdict.DENY


# ---------------------------------------------------------------------------
# 场景 5：独立参考实现逐点核对整张转移表
# ---------------------------------------------------------------------------
def test_exhaustive_transition_table_against_independent_oracle(
        service, sign_policy):
    old_doc = {"version": "v1", "rules": [
        {"id": "read", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["get"], "principals": ["acct/alice"],
         "conditions": [{"attribute": "tls", "op": "Eq", "value": True}]}]}
    new_doc = {"version": "v2", "rules": [
        {"id": "read", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["get"], "principals": ["acct/alice"],
         "conditions": [{"attribute": "tls", "op": "Eq", "value": True}]},
        {"id": "write", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["put"], "principals": ["acct/bob"]}]}
    old_p = parse_policy(old_doc)
    new_p = parse_policy(new_doc)
    # 参考实现消费与解析后等价的规范化文档（前缀已闭合）
    old_norm = {"rules": [r.to_dict() for r in old_p.rules]}
    new_norm = {"rules": [r.to_dict() for r in new_p.rules]}

    res = _diff(service, old_p, new_p, prefixes=["logs/"],
                actions=["get", "put"])

    u = build_universe(
        scope_prefixes=("logs/",),
        scope_actions=frozenset({"get", "put"}),
        policies=[old_p, new_p],
        resource_alphabet=service.cfg.resource_alphabet,
        configured_principals=service.cfg.principals,
        include_anonymous=True,
        max_space_size=service.cfg.max_space_size,
    )

    checked = 0
    mismatches: list = []
    in_scope_points = 0
    for region, request in u.iter_requests():
        if request.action not in ("get", "put"):
            continue
        in_scope_points += 1
        attrs = {k: v for k, v in request.attributes if v is not MISSING}
        plain = {"principal": request.principal, "action": request.action,
                 "resource": request.resource, "attributes": attrs}
        ro, rn = reference_decide(old_norm, plain), reference_decide(new_norm, plain)
        ko = evaluate(old_p, request).verdict.value
        kn = evaluate(new_p, request).verdict.value
        checked += 1
        if (ro, rn) != (ko, kn):
            mismatches.append((plain, (ro, rn), (ko, kn)))

    # 范围内每个点都被核对（不是抽样）
    assert checked == in_scope_points
    assert checked > 0
    assert not mismatches, f"内核与独立参考不一致: {mismatches[:5]}"

    # 独立参考独立确认关键见证
    assert reference_decide(
        old_norm, {"principal": "acct/bob", "action": "put",
                   "resource": "logs/0", "attributes": {}}) == "DENY"
    assert reference_decide(
        new_norm, {"principal": "acct/bob", "action": "put",
                   "resource": "logs/0", "attributes": {}}) == "ALLOW"
    # 未知 tls 时两版都必须 UNKNOWN，而不是允许
    assert reference_decide(
        old_norm, {"principal": "acct/alice", "action": "get",
                   "resource": "logs/0", "attributes": {}}) == "UNKNOWN"

    # 引擎自身的转移计数必须与参考逐点统计一致
    ref_widened = 0
    for region, request in u.iter_requests():
        if request.action not in ("get", "put"):
            continue
        attrs = {k: v for k, v in request.attributes if v is not MISSING}
        plain = {"principal": request.principal, "action": request.action,
                 "resource": request.resource, "attributes": attrs}
        if (reference_decide(old_norm, plain),
                reference_decide(new_norm, plain)) == ("DENY", "ALLOW"):
            ref_widened += 1
    assert res.summary["widened_requests"] == ref_widened
