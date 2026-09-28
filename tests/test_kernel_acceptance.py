"""验收核心：不同请求同键不同响应的反例 → 具体见证；修复键后碰撞消失。

每个断言都核对夹具中【人工编写】的期望（维度/原因码/严重级），
并用 tests/oracle.py 的独立键分组交叉确认，不是被测内核的自证。
"""
from __future__ import annotations

from app.kernel import (
    REASON_IDENTITY_AUTHORIZATION,
    REASON_IDENTITY_COOKIE,
    REASON_VARY_ABSENT_MISSING,
    REASON_VARY_DECLARED_BUT_IGNORED,
    REASON_WILDCARD_IGNORED,
    analyze,
    build_fixed_policy,
    remediate,
)
from app.models import Evidence, Policy
from app.parser import sha256_hex

from . import oracle


def _load(scenario: dict, policy_key: str, policies: dict):
    policy = Policy.model_validate(policies[policy_key])
    evidence = [Evidence.model_validate(e) for e in scenario["evidence"]]
    return policy, evidence


def _check_expected_finding(scenario: dict, result: dict):
    exp = scenario["expected_broken"]
    findings = result["findings"]
    assert len(findings) == exp["findings_count"], (
        f"{scenario['id']}: 期望 {exp['findings_count']} 个发现，实际 {len(findings)}")
    if not exp.get("witness"):
        return findings
    w = exp["witness"]
    f = findings[0]
    pair = f["pair"]
    assert f["reason"] == w["reason"], f["reason"]
    assert f["severity"] == w["severity"]
    assert f["dimension"] == w["dimension"]
    assert sorted([pair["evidence_a"], pair["evidence_b"]]) == sorted(w["evidence"])
    assert pair["response_body_a_sha256"] != pair["response_body_b_sha256"]
    return findings


def test_each_counterexample_then_fix(scenarios, log_record):
    for sid in ["S1-language", "S2-encoding", "S3-authz-identity",
                "S5-vary-star", "S6-missing-vary", "S7-static-ok", "S8-shared-cookie"]:
        sc = next(s for s in scenarios["scenarios"] if s["id"] == sid)
        policy, evidence = _load(sc, sc["policy"], scenarios["policies"])

        broken = analyze(policy, evidence)
        _check_expected_finding(sc, broken)

        # 独立预言机：缺陷策略下手写期望对必须同键
        raw_policy = scenarios["policies"][sc["policy"]]
        groups = oracle.group_by_naive_key(raw_policy, sc["evidence"])
        exp = sc["expected_broken"]
        if exp.get("witness"):
            oracle.assert_expected_pair_collides(groups, exp["witness"]["evidence"])

        # 修复键：同一批证据，碰撞必须消失
        fixed_policy = build_fixed_policy(policy, evidence)
        after = analyze(fixed_policy, evidence)
        assert len(after["findings"]) == sc["expected_fixed"]["findings_count"], (
            f"{sid}: 修复后残留 {[f['reason'] for f in after['findings']]}")
        oracle.assert_expected_pair_separated(
            oracle.group_by_naive_key(fixed_policy.model_dump(mode="json"),
                                      sc["evidence"]),
            [e["id"] for e in sc["evidence"]])
        log_record("scenario", scenario=sid,
                   broken_findings=[f["reason"] for f in broken["findings"]],
                   fixed_findings=len(after["findings"]),
                   rationale=broken["decision_rationale"])


def test_remediate_reports_cleared_witnesses(scenarios, log_record):
    sc = next(s for s in scenarios["scenarios"] if s["id"] == "S3-authz-identity")
    policy, evidence = _load(sc, sc["policy"], scenarios["policies"])
    out = remediate(policy, evidence)

    assert out["collision_gone"] is True
    assert len(out["cleared_witness_ids"]) == 1
    assert out["residual_witness_ids"] == []
    assert out["after"]["findings"] == []
    # 修复后两个身份的键分量必须各自带上不同 Authorization
    comps = out["after"]["derived_keys"]
    a = comps["me-alice"]["identity"]["Authorization"]["value"]
    b = comps["me-bob"]["identity"]["Authorization"]["value"]
    assert a != b and "synthetic-alice" in a and "synthetic-bob" in b
    log_record("remediation", **{
        "cleared": out["cleared_witness_ids"],
        "residual": out["residual_witness_ids"],
        "collision_gone": out["collision_gone"],
    })


def test_private_cache_binds_cookie_even_when_policy_omits_it(scenarios, log_record):
    """私有缓存：策略漏配 Cookie 也不允许跨会话复用——键隐式绑定身份。"""
    sc = next(s for s in scenarios["scenarios"] if s["id"] == "S4-private-cookie")
    policy, evidence = _load(sc, sc["policy"], scenarios["policies"])
    result = analyze(policy, evidence)

    assert result["findings"] == []
    comps = result["derived_keys"]
    assert comps["cart-s1"]["identity"]["Cookie"]["source"] == "private_implicit"
    assert comps["cart-s2"]["identity"]["Cookie"]["source"] == "private_implicit"
    # 独立预言机也确认私有键不同
    groups = oracle.group_by_naive_key(scenarios["policies"]["broken_private"],
                                       sc["evidence"])
    assert groups == {}, groups
    log_record("private-isolation", keys=list(comps))


def test_wildcard_vs_absent_are_classified_differently(scenarios):
    """Vary 通配与缺失头必须落到不同原因码。"""
    star = next(s for s in scenarios["scenarios"] if s["id"] == "S5-vary-star")
    absent = next(s for s in scenarios["scenarios"] if s["id"] == "S6-missing-vary")
    r_star = analyze(*_load(star, "broken_shared", scenarios["policies"]))
    r_absent = analyze(*_load(absent, "broken_shared", scenarios["policies"]))
    assert r_star["findings"][0]["reason"] == REASON_WILDCARD_IGNORED
    assert r_absent["findings"][0]["reason"] == REASON_VARY_ABSENT_MISSING
    # 响应级 Vary 状态也分别记录
    assert r_star["response_vary_state"]["star-a"] == "wildcard"
    assert r_absent["response_vary_state"]["absent-en"] == "absent"


def test_identical_bodies_never_collide_even_under_broken_policy(scenarios):
    """阴性对照：同键但响应字节相同 → 不是碰撞（拒绝误报）。"""
    sc = next(s for s in scenarios["scenarios"] if s["id"] == "S7-static-ok")
    result = analyze(*_load(sc, "broken_shared", scenarios["policies"]))
    assert result["collision_groups"] == 1  # 确实同键
    assert result["findings"] == []         # 但没有受害者


def test_shared_storage_default_forbids_identity_responses():
    """共享缓存默认拒存带 Authorization/Set-Cookie 的响应（存储侧闸门）。"""
    p = Policy(name="strict-shared", cache_scope="shared",
               allow_storing_authorization_response=False,
               allow_storing_cookie_response=False)
    ev_authz = Evidence.model_validate({
        "id": "x1",
        "request": {"method": "get", "scheme": "https", "host": "h", "path": "/",
                    "headers": {"Authorization": "Bearer t"}},
        "response": {"status": 200,
                     "headers": {"Cache-Control": "max-age=10"},
                     "body": "x", "body_sha256": sha256_hex("x")},
    })
    ev_cookie = Evidence.model_validate({
        "id": "x2",
        "request": {"method": "get", "scheme": "https", "host": "h", "path": "/",
                    "headers": {}},
        "response": {"status": 200,
                     "headers": {"Cache-Control": "max-age=10", "Set-Cookie": "sid=t"},
                     "body": "x", "body_sha256": sha256_hex("x")},
    })
    out = analyze(p, [ev_authz, ev_cookie])
    assert out["cacheable_decisions"] == {"x1": False, "x2": False}


def test_reason_codes_are_stable_strings():
    assert REASON_IDENTITY_AUTHORIZATION == "identity_authorization_cross_identity"
    assert REASON_IDENTITY_COOKIE == "identity_cookie_cross_identity"
    assert REASON_VARY_DECLARED_BUT_IGNORED == "response_vary_declared_but_policy_ignored"
