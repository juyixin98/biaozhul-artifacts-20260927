"""独立预言测试。

三层独立来源交叉验证，避免"参考答案全部由被测核心自身生成"：

1. ``fixtures/expected.json`` 中**手写**的预期 verdict / code / rule；
2. 本文件用**标准库** :mod:`ipaddress` 独立重算的地址分类（不 import 内核
   的策略/规范化逻辑来当标准答案）；
3. 被测内核的实际决策链。

任何两层不一致即测试失败。
"""

from __future__ import annotations

import ipaddress

import pytest

# ---------------------------------------------------------------------------
# 独立分类器：仅依赖标准库，与内核实现完全分离
# ---------------------------------------------------------------------------
def classify(ip: str) -> str:
    # 处理需要"展开/解包"的输入：手工去 mapped 前缀
    v = ip.strip()
    mapped = False
    try:
        addr = ipaddress.ip_address(v)
    except ValueError:
        return "unparseable"
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
        mapped = True
    if addr.is_loopback:
        return "loopback_after_unwrap" if mapped else "loopback"
    if addr.is_link_local:
        return "link_local_after_unwrap" if mapped else "link_local"
    # RFC5737 / RFC2544 文档保留地址：测试语义中代表"外部诱饵公网地址"。
    # 标准库将其标为 is_private，这里用独立的保留网段集合显式区分，
    # 使预言不依赖内核实现也不依赖 is_private 的默认口径。
    documentation = (
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("198.51.100.0/24"),
        ipaddress.ip_network("203.0.113.0/24"),
        ipaddress.ip_network("198.18.0.0/15"),
    )
    if any(addr in net for net in documentation):
        return "public"
    if addr.is_private and not addr.is_global:
        return "private"
    if addr.is_global:
        return "public"
    return "special"


# 数字/进制写法 → 标准库能识别的展开值（独立于内核实现的宽松解析）
_INTEGER_FORMS = {
    "2130706433": "127.0.0.1",
    "0x7f000001": "127.0.0.1",
    "0177.0.0.1": "127.0.0.1",
}


def expand_form(ip: str) -> str:
    return _INTEGER_FORMS.get(ip, ip)


# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case_id", [
    "allow-local-origin", "deny-metadata-direct", "deny-metadata-name",
    "deny-rebind-mixed-set", "deny-ipv4-mapped-ipv6", "deny-mapped-meta",
    "deny-userinfo", "deny-integer-ip", "deny-hex-ip", "deny-octal-ip",
    "deny-mixed-private-set", "deny-default-public", "deny-private-name",
    "input-bad-scheme", "input-percent-host", "input-raw-ipv6",
    "input-port-range", "redirect-allow-to-meta", "redirect-allow-to-rebind",
    "redirect-allow-to-mapped",
])
def test_oracle_self_consistency(expected_cases, case_id):
    """先验证手写夹具与标准库分类一致（夹具自身的正确性）。"""
    c = next(x for x in expected_cases if x["id"] == case_id)
    for raw, expect in c.get("expect_classification", {}).items():
        if expect in {"127.0.0.1"}:  # 形如 "数字形式" -> "展开值" 的映射
            assert expand_form(raw) == expect, f"{c['id']}: {raw} 应展开为 {expect}"
        else:
            got = classify(expand_form(raw))
            assert got == expect, (
                f"{c['id']}: 标准库分类 {raw!r}={got!r}，手写期望 {expect!r}"
            )


def test_expected_cases_have_independent_fields(expected_cases):
    """每个用例都必须带手写的独立断言字段（禁止空泛的"接口能调用"）。"""
    for c in expected_cases:
        assert c.get("expected_verdict") in {"allow", "deny", "input_error",
                                             "state_conflict", "resource_exhausted"}
        if c["expected_verdict"] != "allow":
            assert c.get("expected_code"), f"{c['id']} 缺少 expected_code"
        assert c.get("why")
        assert "connect_should_happen" in c


def _redirect_routes():
    cf = __import__("conftest")
    return {
        "/ok": cf.ok(),
        "/redirect-meta": cf.redirect("http://metadata.example/"),
        "/redirect-rebind": cf.redirect("http://rebind.example:18080/x"),
        "/redirect-mapped": cf.redirect("http://mapped.example/x"),
    }


def test_deny_cases_never_connect(factory, expected_cases):
    """核心断言：每个 deny 用例，被禁 IP 从未出现在连接尝试记录中。"""
    kernel, connector = factory(routes=_redirect_routes())

    for c in expected_cases:
        if c["expected_verdict"] != "deny":
            continue
        connector.attempts.clear()
        result = kernel.fetch(c["url"])
        assert result["final_verdict"] == "deny", (
            f"{c['id']}: 期望 deny，实际 {result['final_verdict']}"
        )
        # 被禁地址从未连接
        forbidden = set(c.get("must_not_connect_any", []))
        connected = set(connector.attempted_peers())
        overlap = forbidden & connected
        assert not overlap, f"{c['id']}: 被禁地址竟被连接: {overlap}"

        # 凡是不允许任何连接的用例，连接记录必须为空
        if not c.get("connect_should_happen"):
            assert connector.attempts == [], (
                f"{c['id']}: 预期零连接，实际尝试 {connector.attempts}"
            )


def test_deny_cases_error_code_and_rule(factory, expected_cases):
    """断言具体失败类别(code)与命中规则，而不仅是"拒绝了"。"""
    kernel, _connector = factory(routes=_redirect_routes())
    for c in expected_cases:
        if c["expected_verdict"] != "deny":
            continue
        result = kernel.fetch(c["url"])
        err = result["error"]
        assert err["code"] == c["expected_code"], (
            f"{c['id']}: code {err['code']} != {c['expected_code']}\n决策链: {result['hops']}"
        )
        # 找到 policy 阶段的命中规则
        rule_ids = {
            h["matched"]["rule_id"]
            for h in result["hops"]
            if h.get("matched")
        }
        assert c["expected_rule"] in rule_ids, (
            f"{c['id']}: 期望命中规则 {c['expected_rule']}，实际 {rule_ids}"
        )


def test_input_error_cases(factory, expected_cases):
    kernel, connector = factory()
    for c in expected_cases:
        if c["expected_verdict"] != "input_error":
            continue
        connector.attempts.clear()
        result = kernel.fetch(c["url"])
        assert result["final_verdict"] == "input_error", f"{c['id']}: {result['final_verdict']}"
        assert result["error"]["code"] == c["expected_code"], (
            f"{c['id']}: {result['error']['code']} != {c['expected_code']}"
        )
        assert connector.attempts == [], f"{c['id']}: 输入错误不应产生连接"
