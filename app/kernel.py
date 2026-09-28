"""安全内核：缓存键派生、身份隔离判定、碰撞见证与修复键核验。

内核的立场是受限的：它只判断“给定策略是否覆盖了请求之间的差异”，
不声称自动理解任意业务隐私。Authorization / Cookie 有专门的身份规则：
共享缓存里，同键但身份不同的重用永远判 critical；私有缓存的键隐式绑定身份。

键派生（确定性元组，JSON 仅用于展示）：
    scope | method | scheme | host | path | canonical_query
    | 每个生效 Vary 头的值
    | 身份分量（Authorization / Cookie，按作用域与策略）
响应 Vary:* 被策略忽略时不贡献任何分量——这正是反例要暴露的缺陷。
"""
from __future__ import annotations

from typing import Any

from .models import Evidence, Policy
from .parser import (
    VARY_ABSENT,
    VARY_EXPLICIT,
    VARY_WILDCARD,
    canonical_query,
    is_response_cacheable,
    parse_vary,
    witness_id,
)

# 维度常量
DIM_AUTHORIZATION = "Authorization"
DIM_COOKIE = "Cookie"
DIM_VARY_WILDCARD = "*"

# 原因常量（测试按这些稳定 code 断言）
REASON_IDENTITY_AUTHORIZATION = "identity_authorization_cross_identity"
REASON_IDENTITY_COOKIE = "identity_cookie_cross_identity"
REASON_WILDCARD_IGNORED = "response_vary_wildcard_ignored"
REASON_VARY_DECLARED_BUT_IGNORED = "response_vary_declared_but_policy_ignored"
REASON_VARY_ABSENT_MISSING = "vary_absent_and_policy_missing_dimension"
REASON_DIMENSION_NOT_COVERED = "dimension_covered_by_neither_policy_nor_vary"

IDENTITY_HEADERS = (DIM_AUTHORIZATION, DIM_COOKIE)


def _hv(ev: Evidence, name: str) -> str:
    return ev.request.headers.get(name, "").strip()


def _response_has_set_cookie(ev: Evidence) -> bool:
    return "Set-Cookie" in ev.response.headers


def evaluate_cacheability(policy: Policy, ev: Evidence) -> tuple[bool, str]:
    """返回 (是否可缓存, 理由)。理由写入审计日志与中间状态。"""
    if not is_response_cacheable(ev.response.status, ev.response.headers):
        return False, f"http_undeclarable: 状态 {ev.response.status} 且无缓存指令，或 Cache-Control: no-store"

    state, _ = parse_vary(ev.response.headers)
    if state == VARY_WILDCARD and policy.respect_response_vary:
        if policy.vary_wildcard_mode == "forbid":
            return False, "response Vary:* 且策略尊重它：通配响应禁止进入共享/重用缓存"
        return False, "response Vary:* 且 vary_wildcard_mode=uncacheable：按不可缓存处理"

    if policy.cache_scope == "shared":
        if _hv(ev, DIM_AUTHORIZATION) and not policy.allow_storing_authorization_response:
            return False, "shared 缓存默认禁止存储带 Authorization 的响应（策略未显式放行）"
        if _response_has_set_cookie(ev) and not policy.allow_storing_cookie_response:
            return False, "shared 缓存默认禁止存储带 Set-Cookie 的响应（策略未显式放行）"

    return True, "cacheable: 响应声明允许存储，且未命中身份存储禁令"


def effective_vary_for(policy: Policy, ev: Evidence) -> list[str]:
    """生效的请求维度列表：策略声明 ∪（在尊重时）响应 Vary 声明。"""
    state, names = parse_vary(ev.response.headers)
    chosen: list[str] = list(policy.vary_headers)
    if policy.respect_response_vary and state == VARY_EXPLICIT:
        for n in names:
            if n not in chosen:
                chosen.append(n)
    return chosen


def _identity_in_key(policy: Policy, eff_vary: list[str], header: str) -> bool:
    if header in eff_vary:
        return True
    if header == DIM_AUTHORIZATION:
        return policy.include_authorization
    return policy.include_cookie


def derive_key(policy: Policy, ev: Evidence, eff_vary: list[str]) -> tuple[tuple, dict[str, Any]]:
    """派生缓存键。返回 (可哈希元组, 展示用分量字典)。"""
    query_c = canonical_query(ev.request.query)
    components: dict[str, Any] = {
        "scope": policy.cache_scope,
        "method": ev.request.method,
        "scheme": ev.request.scheme,
        "host": ev.request.host,
        "path": ev.request.path,
        "query_canonical": query_c,
        "vary": {},
        "identity": {},
    }
    key: list[Any] = [policy.cache_scope, ev.request.method, ev.request.scheme,
                      ev.request.host, ev.request.path, query_c]

    for h in eff_vary:
        val = _hv(ev, h)
        components["vary"][h] = val
        key.append(("vary", h, val))

    # 身份分量：私有缓存始终隐式绑定；共享缓存取决于策略是否把身份纳入键
    for h in IDENTITY_HEADERS:
        present = bool(_hv(ev, h))
        if not present:
            continue
        if policy.cache_scope == "private":
            bound = True
            source = "private_implicit"
        else:
            bound = _identity_in_key(policy, eff_vary, h)
            source = "policy_keyed" if bound else "not_keyed"
        if bound:
            val = _hv(ev, h)
            components["identity"][h] = {"value": val, "source": source}
            key.append(("identity", h, val))
        else:
            components["identity"][h] = {"value": _hv(ev, h), "source": source}

    return tuple(key), components


def _snapshot_request(ev: Evidence) -> dict[str, Any]:
    return {
        "id": ev.id,
        "method": ev.request.method,
        "scheme": ev.request.scheme,
        "host": ev.request.host,
        "path": ev.request.path,
        "query": ev.request.query,
        "headers": dict(ev.request.headers),
    }


def _differing_headers(a: Evidence, b: Evidence) -> list[str]:
    keys = set(a.request.headers) | set(b.request.headers)
    return sorted(h for h in keys if a.request.headers.get(h, "").strip()
                  != b.request.headers.get(h, "").strip())


def _pair_finding(policy: Policy, a: Evidence, b: Evidence,
                  comp_a: dict, comp_b: dict) -> dict[str, Any] | None:
    """同一键组内的一对证据：判定是否构成碰撞见证。"""
    authz_diff = _hv(a, DIM_AUTHORIZATION) != _hv(b, DIM_AUTHORIZATION)
    cookie_diff = _hv(a, DIM_COOKIE) != _hv(b, DIM_COOKIE)
    body_diff = a.response.body_sha256 != b.response.body_sha256

    state_a, names_a = parse_vary(a.response.headers)
    state_b, names_b = parse_vary(b.response.headers)
    differing = [h for h in _differing_headers(a, b) if h not in IDENTITY_HEADERS]

    # 1) 身份隔离：共享缓存里同键跨 Authorization 身份 —— 永远 critical
    if policy.cache_scope == "shared" and authz_diff:
        dim, reason = DIM_AUTHORIZATION, REASON_IDENTITY_AUTHORIZATION
    elif policy.cache_scope == "shared" and cookie_diff:
        dim, reason = DIM_COOKIE, REASON_IDENTITY_COOKIE
    else:
        # 2) Vary:* 被策略忽略：只要请求头存在差异，通配声明本身即受害证据
        wildcard_ignored = (
            not policy.respect_response_vary
            and (state_a == VARY_WILDCARD or state_b == VARY_WILDCARD)
            and (differing or authz_diff or cookie_diff)
        )
        if wildcard_ignored:
            dim = differing[0] if differing else DIM_VARY_WILDCARD
            return _finding("high", dim, REASON_WILDCARD_IGNORED, a, b, comp_a, comp_b,
                            extra={"response_vary_state": [state_a, state_b]})

        # 3) 非同身份问题：响应体必须确实不同，重用才造成可观察的错误响应
        if not body_diff:
            return None

        if not differing:
            # 体不同但所有请求头相同 —— 不属于键覆盖问题（可能是源站不稳定），不出证
            return None

        declared = set(names_a) | set(names_b)
        listed = [h for h in differing if h in declared]
        if listed:
            dim = listed[0]
            reason = REASON_VARY_DECLARED_BUT_IGNORED
        elif state_a == VARY_ABSENT and state_b == VARY_ABSENT:
            dim = differing[0]
            reason = REASON_VARY_ABSENT_MISSING
        else:
            dim = differing[0]
            reason = REASON_DIMENSION_NOT_COVERED
        return _finding("medium", dim, reason, a, b, comp_a, comp_b,
                        extra={"uncovered_dimensions": differing})

    return _finding("critical", dim, reason, a, b, comp_a, comp_b)


def _finding(severity: str, dimension: str, reason: str, a: Evidence, b: Evidence,
             comp_a: dict, comp_b: dict, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    wid = witness_id(a.id, b.id, dimension)
    pair = {
        "witness_id": wid,
        "evidence_a": a.id,
        "evidence_b": b.id,
        "dimension": dimension,
        "reason": reason,
        "severity": severity,
        "request_a": _snapshot_request(a),
        "request_b": _snapshot_request(b),
        "key_components_a": comp_a,
        "key_components_b": comp_b,
        "response_body_a_sha256": a.response.body_sha256,
        "response_body_b_sha256": b.response.body_sha256,
    }
    if extra:
        pair.update(extra)
    return {"category": "cache_collision", "dimension": dimension,
            "reason": reason, "severity": severity, "pair": pair}


def analyze(policy: Policy, evidence_list: list[Evidence]) -> dict[str, Any]:
    """对一组证据执行完整分析，返回可直接序列化的 AnalysisView 结构。"""
    decisions: dict[str, bool] = {}
    rationale: list[str] = []
    eff_vary_map: dict[str, list[str]] = {}
    vary_state_map: dict[str, str] = {}
    derived: dict[str, dict[str, Any]] = {}

    groups: dict[tuple, list[tuple[Evidence, dict[str, Any]]]] = {}

    for ev in evidence_list:
        cacheable, why = evaluate_cacheability(policy, ev)
        decisions[ev.id] = cacheable
        state, _ = parse_vary(ev.response.headers)
        vary_state_map[ev.id] = state
        rationale.append(f"[{ev.id}] cacheable={cacheable} :: {why}")
        if not cacheable:
            continue
        eff = effective_vary_for(policy, ev)
        eff_vary_map[ev.id] = eff
        key_tuple, comp = derive_key(policy, ev, eff)
        comp["cache_key_hex"] = None  # 由 groups 归属后无需摘要；保留槽位说明
        derived[ev.id] = comp
        groups.setdefault(key_tuple, []).append((ev, comp))

    findings: list[dict[str, Any]] = []
    seen_witness: set[str] = set()
    collision_groups = 0
    for key_tuple, members in groups.items():
        if len(members) > 1:
            collision_groups += 1
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, comp_a = members[i]
                b, comp_b = members[j]
                f = _pair_finding(policy, a, b, comp_a, comp_b)
                if f and f["pair"]["witness_id"] not in seen_witness:
                    seen_witness.add(f["pair"]["witness_id"])
                    rationale.append(
                        f"[{a.id} x {b.id}] 同键碰撞 dimension={f['dimension']} "
                        f"reason={f['reason']} severity={f['severity']}"
                    )
                    findings.append(f)

    findings.sort(key=lambda f: ({"critical": 0, "high": 1, "medium": 2}[f["severity"]],
                                 f["pair"]["witness_id"]))
    return {
        "policy_name": policy.name,
        "cache_scope": policy.cache_scope,
        "analyzed_evidence": len(evidence_list),
        "collision_groups": collision_groups,
        "findings": findings,
        "cacheable_decisions": decisions,
        "effective_vary": eff_vary_map,
        "response_vary_state": vary_state_map,
        "derived_keys": derived,
        "decision_rationale": rationale,
    }


def build_fixed_policy(policy: Policy,
                       evidence_list: list[Evidence] | None = None) -> Policy:
    """从有缺陷的策略构造“修复键”策略：
    - 尊重响应 Vary（含通配的禁用语义）；
    - 共享缓存把 Authorization / Cookie 显式纳入键；
    - 源站【缺失 Vary】时，把同键不同响应中实际见证到差异的请求维度补进键
      （只依据给定证据中观察到的差异，不臆测业务隐私）；
    - 保留原策略已声明的维度与存储放行（放行使其可证：靠键分离而非拒存消除碰撞）。
    """
    vary = list(policy.vary_headers)
    if evidence_list:
        # 用原（缺陷）策略派键，找出仍同键的证据对，收集其差异请求头
        groups: dict[tuple, list[Evidence]] = {}
        for ev in evidence_list:
            cacheable, _ = evaluate_cacheability(policy, ev)
            if not cacheable:
                continue
            eff = effective_vary_for(policy, ev)
            key_tuple, _ = derive_key(policy, ev, eff)
            groups.setdefault(key_tuple, []).append(ev)
        for members in groups.values():
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    keys = set(a.request.headers) | set(b.request.headers)
                    for h in keys:
                        if h in IDENTITY_HEADERS:
                            continue
                        if (a.request.headers.get(h, "").strip()
                                != b.request.headers.get(h, "").strip() and h not in vary):
                            vary.append(h)

    updates: dict[str, Any] = {
        "name": policy.name + "::fixed-key",
        "respect_response_vary": True,
        "vary_wildcard_mode": "forbid",
        "vary_headers": vary,
    }
    if policy.cache_scope == "shared":
        updates["include_authorization"] = True
        updates["include_cookie"] = True
    return policy.model_copy(update=updates)


def remediate(policy: Policy, evidence_list: list[Evidence]) -> dict[str, Any]:
    """用修复键策略重放同一批证据，核对碰撞是否消失。"""
    before = analyze(policy, evidence_list)
    fixed = build_fixed_policy(policy, evidence_list)
    after = analyze(fixed, evidence_list)
    before_ids = {f["pair"]["witness_id"] for f in before["findings"]}
    after_ids = {f["pair"]["witness_id"] for f in after["findings"]}
    return {
        "broken_policy_name": policy.name,
        "fixed_policy": fixed.model_dump(),
        "before": before,
        "after": after,
        "cleared_witness_ids": sorted(before_ids - after_ids),
        "residual_witness_ids": sorted(after_ids),
        "collision_gone": len(after["findings"]) == 0,
    }
