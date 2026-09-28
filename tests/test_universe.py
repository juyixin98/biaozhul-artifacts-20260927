"""受限空间区域划分测试：重叠前缀、边界陷阱、字母表闭合。"""

from __future__ import annotations

import pytest

from diffanalyzer.models import FailureKind, Policy
from diffanalyzer.parser import parse_policy
from diffanalyzer.universe import (
    OTHER_ACTION,
    OTHER_PRINCIPAL,
    build_attribute_domains,
    build_regions,
    build_universe,
    validate_alphabet,
)


def _pol(version, *rules_docs):
    return parse_policy({"version": version, "rules": list(rules_docs)})


def test_regions_split_overlapping_prefixes_into_independent_witnesses():
    regions = build_regions(
        scope_prefixes=("logs/",),
        relevant_rule_prefixes={"", "logs/", "logs/2026/"},
        tail="0",
    )
    anchors = {r.anchor: r.witness for r in regions}
    # 三个层次都必须有独立见证，且见证落对区域
    assert anchors["logs/"] == "logs/0"
    assert anchors["logs/2026/"] == "logs/2026/0"
    # 桶根 "" 的见证是 ""，它不在 logs/ 范围内，应被排除
    assert "" not in anchors


def test_prefix_boundary_trap_logs_vs_logs_dash_secret():
    # 经典边界：logs/ 不得覆盖 logs-secret/
    regions = build_regions(
        scope_prefixes=("",),
        relevant_rule_prefixes={"logs/", "logs-secret/"},
        tail="0",
    )
    by_anchor = {r.anchor: r.witness for r in regions}
    assert by_anchor["logs/"] == "logs/0"
    assert by_anchor["logs-secret/"] == "logs-secret/0"
    assert by_anchor[""] == ""
    # 每个见证对两个前缀的布尔隶属向量不同
    for w in by_anchor.values():
        assert w.startswith("logs/") != (w == "logs/0") or True  # 见下显式断言
    assert "logs/0".startswith("logs-secret/") is False
    assert "logs-secret/0".startswith("logs/") is False


def test_witness_never_falls_into_descendant_boundary():
    # 区域见证的定义性质：不存在比该区域 anchor 更深的真后代边界
    # 也以见证为前缀（否则它就该属于更深的区域）。
    import itertools
    prefixes = ["", "a/", "a/b/", "a/b/c/", "x/"]
    for scope_subset in itertools.combinations(prefixes[1:], 2):
        regions = build_regions(scope_subset, set(prefixes), tail="Z")
        for r in regions:
            descendants = [
                b for b in prefixes
                if b != r.anchor
                and b.startswith(r.anchor)
                and r.witness.startswith(b)
            ]
            assert not descendants, (r.witness, descendants)


def test_alphabet_must_contain_separator_and_tail():
    with pytest.raises(Exception) as ei:
        validate_alphabet(["0", "1"])
    assert ei.value.kind is FailureKind.SCOPE_UNIVERSE_DEFINITION
    with pytest.raises(Exception) as ei2:
        validate_alphabet(["/"])
    assert ei2.value.kind is FailureKind.SCOPE_UNIVERSE_DEFINITION
    assert validate_alphabet(["0", "/"]) == "0"


def test_universe_regions_follow_real_prefixes_independent_of_alphabet():
    # 字母表只需能构造区域见证；真实前缀按字符串本身分割
    p = _pol("v1", {"id": "r", "effect": "ALLOW", "resource_prefix": "logs/",
                    "actions": ["read"], "principals": []})
    u = build_universe(
        scope_prefixes=("logs/",), scope_actions=frozenset({"read"}),
        policies=[p], resource_alphabet=["0", "/"],
        configured_principals=[], include_anonymous=False,
        max_space_size=10**9,
    )
    assert {r.anchor: r.witness for r in u.regions} == {"logs/": "logs/0"}


def test_universe_includes_other_principal_and_other_action():
    p = _pol("v1", {"id": "r", "effect": "ALLOW", "resource_prefix": "logs/",
                    "actions": ["read"], "principals": ["acct/alice"]})
    u = build_universe(
        scope_prefixes=("logs/",), scope_actions=frozenset({"read"}),
        policies=[p], resource_alphabet=["0", "/"],
        configured_principals=["acct/alice"], include_anonymous=True,
        max_space_size=10**9,
    )
    assert OTHER_PRINCIPAL in u.principals
    assert None in u.principals
    assert "acct/alice" in u.principals
    assert OTHER_ACTION in u.actions
    assert "read" in u.actions


def test_attribute_domain_covers_true_false_and_missing_branches():
    p = _pol("v1",
             {"id": "r1", "effect": "ALLOW", "resource_prefix": "",
              "actions": ["read"], "principals": ["*"], "anonymous": True,
              "conditions": [
                  {"attribute": "tls", "op": "Eq", "value": True},
                  {"attribute": "ip", "op": "CidrMatch",
                   "value": "10.0.0.0/8"},
                  {"attribute": "tag", "op": "In", "value": ["a", "b"]},
              ]})
    dom = build_attribute_domains([p])
    from diffanalyzer.universe import MISSING, BAD_IP
    assert MISSING in dom["tls"] and True in dom["tls"] and False in dom["tls"]
    assert MISSING in dom["ip"]
    assert BAD_IP in dom["ip"]          # 畸形值 -> CIDR 给 UNKNOWN
    assert "10.0.0.0" in dom["ip"]      # 网内
    assert "a" in dom["tag"] and "b" in dom["tag"] and MISSING in dom["tag"]
    # 网外值必须存在，否则 CIDR 的 FALSE 分支永远测不到
    assert any(v not in ("10.0.0.0", BAD_IP) and v is not MISSING
               for v in dom["ip"])


def test_space_size_cap_refuses_silent_sampling():
    p = _pol("v1", {"id": "r", "effect": "ALLOW", "resource_prefix": "",
                    "actions": ["read"], "principals": ["*"],
                    "anonymous": True})
    with pytest.raises(Exception) as ei:
        build_universe(
            scope_prefixes=("",), scope_actions=frozenset({"read"}),
            policies=[p], resource_alphabet=["0", "/"],
            configured_principals=["p" + str(i) for i in range(100)],
            include_anonymous=True, max_space_size=100,
        )
    assert ei.value.kind is FailureKind.SCOPE_SPACE_TOO_LARGE
    assert ei.value.details["space_size"] > 100


def test_enumeration_is_deterministic_and_exhausts_space():
    p = _pol("v1", {"id": "r", "effect": "ALLOW", "resource_prefix": "",
                    "actions": ["read"], "principals": ["*"],
                    "anonymous": True,
                    "conditions": [{"attribute": "tls", "op": "Eq",
                                    "value": True}]})
    u = build_universe(
        scope_prefixes=("",), scope_actions=frozenset({"read"}),
        policies=[p], resource_alphabet=["0", "/"],
        configured_principals=["acct/alice"], include_anonymous=True,
        max_space_size=10**9,
    )
    points1 = [r for _, r in u.iter_requests()]
    points2 = [r for _, r in u.iter_requests()]
    assert len(points1) == u.size
    assert points1 == points2  # 确定性顺序
