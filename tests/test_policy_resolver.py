"""策略规则：首条命中、混合地址集合、grant 顺序、解析错误类别。"""
from __future__ import annotations

import pytest

from app.contracts import FailureKind, Reason
from app.policy import Policy
from app.resolver import ControlledResolver


def test_policy_first_match_wins(zones_resolver, base_policy):
    # grant（allow, tag documentation）在文件 deny-documentation 之前
    policy = base_policy.with_grants([{
        "id": "g-doc", "action": "allow",
        "host": "mapped-public.test", "scheme": "http", "port": 80,
        "tag_any": ["documentation"],
    }])
    from app.urlparse import parse_target

    parsed = parse_target("http://mapped-public.test/")
    resolved = zones_resolver.clone().resolve(
        parsed.normalized_for_lookup, 80, host_kind=parsed.host_kind
    )
    decision = policy.evaluate(parsed, resolved.addresses)
    assert decision.allowed is True
    assert decision.chosen is not None
    assert decision.chosen.rule_id == "g-doc"


def test_default_deny_for_unknown_public(base_policy, zones_resolver):
    from app.urlparse import parse_target

    parsed = parse_target("http://8.8.8.8/")
    resolved = zones_resolver.resolve(parsed.normalized_for_lookup, 80, host_kind=parsed.host_kind)
    assert resolved.addresses[0].tags == frozenset()  # 无特殊标签
    d = base_policy.evaluate(parsed, resolved.addresses)
    assert d.allowed is False
    assert d.reason == Reason.IP_BLOCKED
    assert d.decisions[0].rule_id == "deny-public-default"


def test_mixed_allow_and_deny_candidates_denies_whole(base_policy, zones_resolver):
    # tag-scoped grant：只放行 documentation 标签候选，private 仍 deny
    policy = base_policy.with_grants([{
        "id": "g-doc-only", "action": "allow",
        "host": "mixed-allow-deny.test", "scheme": "http", "port": 80,
        "tag_any": ["documentation"],
    }])
    from app.urlparse import parse_target

    parsed = parse_target("http://mixed-allow-deny.test/")
    resolved = zones_resolver.clone().resolve(
        parsed.normalized_for_lookup, 80, host_kind="dns"
    )
    ips = {a.canonical_ip: a for a in resolved.addresses}
    assert set(ips) == {"203.0.113.10", "10.0.0.5"}
    d = policy.evaluate(parsed, resolved.addresses)
    assert d.allowed is False
    assert d.reason == Reason.MIXED_CANDIDATES
    actions = {x.ip: (x.action, x.rule_id) for x in d.decisions}
    assert actions["203.0.113.10"][0] == "allow"
    assert actions["10.0.0.5"][0] == "deny"
    assert d.chosen is None


def test_all_internal_denied_explicit(base_policy, zones_resolver):
    from app.urlparse import parse_target

    parsed = parse_target("http://multi-meta.test/")
    resolved = zones_resolver.clone().resolve("multi-meta.test", 80, host_kind="dns")
    d = base_policy.evaluate(parsed, resolved.addresses)
    assert d.reason == Reason.IP_BLOCKED
    assert all(x.action == "deny" for x in d.decisions)


def test_grant_isolation_does_not_mutate_base(base_policy):
    p1 = base_policy.with_grants([{"id": "g1", "action": "allow", "host": "a.test"}])
    p2 = base_policy.with_grants(None)
    assert any(r.id == "g1" for r in p1.rules)
    assert all(r.id != "g1" for r in p2.rules)
    assert all(r.id != "g1" for r in base_policy.rules)


def test_policy_file_rejects_bad_shape(tmp_path):
    bad = tmp_path / "rules.json"
    bad.write_text('{"rules": [{"id": "x", "action": "maybe"}]}')
    with pytest.raises(Exception) as ei:
        Policy.from_file(bad)
    assert ei.value.kind == FailureKind.INPUT_ERROR


# --- resolver ---------------------------------------------------------------

def test_resolver_literal_does_not_consume_call(zones_resolver):
    rt = zones_resolver.resolve("127.0.0.1", 80, host_kind="ipv4")
    assert rt.source == "literal"
    assert rt.lookup_attempts == 0
    assert rt.addresses[0].canonical_ip == "127.0.0.1"
    assert zones_resolver.calls == ()


def test_resolver_scripted_rebinding_advances_one_call_per_resolve():
    resolver = ControlledResolver.from_file(
        __import__("pathlib").Path("fixtures/dns/zones.json")
    )
    first = resolver.resolve("rebind-public-then-meta.test", 80, host_kind="dns")
    second = resolver.resolve("rebind-public-then-meta.test", 80, host_kind="dns")
    third = resolver.resolve("rebind-public-then-meta.test", 80, host_kind="dns")
    assert first.addresses[0].canonical_ip == "203.0.113.10"
    assert second.addresses[0].canonical_ip == "169.254.169.254"
    # 脚本耗尽停在最后一帧
    assert third.addresses[0].canonical_ip == "169.254.169.254"
    assert [c.step for c in resolver.calls] == [0, 1, 2]
    assert len(resolver.calls) == 3


def test_resolver_nxdomain_is_computation_failed(zones_resolver):
    with pytest.raises(Exception) as ei:
        zones_resolver.resolve("nxdomain.test", 80, host_kind="dns")
    assert ei.value.kind == FailureKind.COMPUTATION_FAILED
    assert ei.value.reason == Reason.DNS_NAME_NOT_FOUND


def test_resolver_temporary_error(zones_resolver):
    with pytest.raises(Exception) as ei:
        zones_resolver.resolve("temporary-dns.test", 80, host_kind="dns")
    assert ei.value.reason == Reason.DNS_TEMPORARY


def test_resolver_unknown_host(zones_resolver):
    with pytest.raises(Exception) as ei:
        zones_resolver.resolve("not-in-zones.test", 80, host_kind="dns")
    assert ei.value.reason == Reason.DNS_NAME_NOT_FOUND


def test_resolver_clone_independent_counts(zones_resolver):
    a = zones_resolver.clone()
    b = zones_resolver.clone()
    a.resolve("rebind-public-then-meta.test", 80, host_kind="dns")
    a.resolve("rebind-public-then-meta.test", 80, host_kind="dns")
    b.resolve("rebind-public-then-meta.test", 80, host_kind="dns")
    assert [c.step for c in a.calls] == [0, 1]
    assert [c.step for c in b.calls] == [0]  # 克隆重置计数 -> 运行间隔离


def test_resolver_classifies_mapped_records(zones_resolver):
    rt = zones_resolver.resolve("mapped-meta.test", 80, host_kind="dns")
    addr = rt.addresses[0]
    assert addr.family == "ipv4"
    assert addr.canonical_ip == "169.254.169.254"
    assert "link-local" in addr.tags
    assert addr.unwrapped_from is not None
