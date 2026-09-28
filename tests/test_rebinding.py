"""DNS 重绑定专项。

用受控夹具确定性地复现：

* 同一名字在不同跳/不同次调用中，解析起点轮转（模拟 TOC-TOU）；
* 无论轮转从哪个答案开始，返回集合始终同时含"可允许的本机"与内网，
  内核必须在**集合层面**拒绝，而不是只看第一个答案——否则攻击者只需让
  "校验时第一个答案是本机、连接时切到元数据"即可绕过。

关键安全断言
============
1. 集合中**任何**候选都不被连接（含这一跳本可允许的那个）；
2. 决策链记录了全部候选与"第 N 次解析"游标，可重放；
3. pin 一旦固定，连接器拿到的是 IP 字面量，主机名不参与二次解析。
"""

from __future__ import annotations


def test_rebind_resolution_sequence_is_observable(zone_table, bundle):
    """夹具层确定性：多次解析起点轮转，模拟校验/连接两时点不同。"""
    from safeproxy.net.resolver import FixtureResolver

    table = dict(zone_table)
    resolver = FixtureResolver(table)
    first = tuple(c.literal for c in resolver.resolve("rebind.example", 8))
    second = tuple(c.literal for c in resolver.resolve("rebind.example", 8))
    assert first == ("127.0.0.1", "169.254.169.254")
    assert second == ("169.254.169.254", "127.0.0.1"), "第 2 次解析必须轮转（重绑定条件）"
    assert resolver.sequence_cursor("rebind.example") == 2


def test_rebind_denies_entire_set_and_connects_nothing(factory):
    kernel, connector = factory()
    result = kernel.fetch("http://rebind.example:18080/loophole")

    assert result["final_verdict"] == "deny"
    assert result["error"]["code"] == "E_POLICY_DENY"
    # 元数据地址在被禁候选中（决定性匹配）
    assert "169.254.169.254" in result["error"]["details"]["denied_candidates"]
    # 决定性规则必须是元数据规则（集合中存在内网即按最严的禁因报告）
    assert result["error"]["details"]["rule_id"] == "deny-metadata-ipv4"
    # 同组里"这一跳本可允许"的 127.0.0.1 也绝不能被连接
    assert result["error"]["details"]["allowed_but_not_connected"] == ["127.0.0.1"]
    # 集合中任何候选都不能被连接
    assert connector.attempts == [], f"混合集拒绝必须零连接，实际 {connector.attempts}"


def test_rebind_even_when_second_resolution_starts_public(factory):
    """轮转后若集合仍含内网，仍须拒绝（不依赖答案顺序）。"""
    kernel, connector = factory()
    # 连续两次抓取（游标推进），两次都必须拒绝
    r1 = kernel.fetch("http://rebind.example:18080/a")
    r2 = kernel.fetch("http://rebind.example:18080/b")
    assert r1["final_verdict"] == "deny" and r2["final_verdict"] == "deny"
    assert connector.attempts == []


def test_mixed_public_private_set_zero_connection(factory):
    kernel, connector = factory()
    result = kernel.fetch("http://mixed.example:18080/")
    assert result["final_verdict"] == "deny"
    assert set(result["error"]["details"]["denied_candidates"]) == {"192.168.1.10"}
    assert result["error"]["details"]["allowed_but_not_connected"] == ["127.0.0.1"]
    assert connector.attempted_peers() == []


def test_decision_chain_lists_all_candidates_and_cursor(factory):
    """决策链必须保留可重放的中间状态：全部候选 + 第几次解析。"""
    kernel, _connector = factory()
    result = kernel.fetch("http://rebind.example:18080/x")
    dns_hops = [h for h in result["hops"] if h["stage"] == "dns"]
    assert dns_hops, "决策链缺少 dns 阶段"
    hop = dns_hops[0]
    assert set(hop["resolved"]) == {"127.0.0.1", "169.254.169.254"}
    assert "1th_resolution" in hop["reason"]
    policy_hop = next(h for h in result["hops"] if h["stage"] == "policy")
    assert "不会被连接" in policy_hop["note"]
