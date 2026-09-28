"""重定向安全专项。

验证：
* 每一跳都从 parse 重新走完整策略（上一跳允许不传导）；
* 允许跳可以真实连接，禁止跳在连接前拦截；
* 允许→禁止：允许的对端被连过，禁止的对端**从未**连接；
* 重定向环 → state_conflict / E_REDIRECT_LOOP；
* 跳数耗尽 → resource_exhausted / E_REDIRECT_BUDGET（两类可区分）。
"""

from __future__ import annotations

from conftest import ok, redirect


def _redirect_routes():
    return {
        "/ok": ok(b"origin-body"),
        "/redirect-ok": redirect("http://127.0.0.1:18080/ok"),
        "/redirect-meta": redirect("http://metadata.example/latest/"),
        "/redirect-rebind": redirect("http://rebind.example:18080/x"),
        "/redirect-mapped": redirect("http://mapped.example/x"),
        "/loop-a": redirect("http://127.0.0.1:18080/loop-b"),
        "/loop-b": redirect("http://127.0.0.1:18080/loop-a"),
        "/many": redirect("http://127.0.0.1:18080/many"),
    }


def test_happy_path_allow_redirect(factory):
    kernel, connector = factory(_redirect_routes())
    result = kernel.fetch("http://127.0.0.1:18080/redirect-ok")
    assert result["final_verdict"] == "allow"
    assert result["status_code"] == 200
    # 两跳：第一跳 redirect-ok，第二跳 ok
    assert len([h for h in result["hops"] if h["stage"] == "connect"]) == 2
    assert connector.attempted_peers() == ["127.0.0.1", "127.0.0.1"]


def test_redirect_allow_then_forbidden_never_connects_forbidden(factory):
    kernel, connector = factory(_redirect_routes())
    result = kernel.fetch("http://127.0.0.1:18080/redirect-meta")

    assert result["final_verdict"] == "deny"
    assert result["error"]["code"] == "E_POLICY_DENY"
    assert result["error"]["details"]["host"] == "metadata.example"

    # 第 1 跳确实连了允许的本机源站
    assert "127.0.0.1" in connector.attempted_peers()
    # 第 2 跳的禁止地址从未连接
    assert "169.254.169.254" not in connector.attempted_peers()
    assert connector.attempted_peers().count("127.0.0.1") == 1

    # 决策链含两跳，第二跳 policy=deny，且第一跳记录了 Location
    hops = result["hops"]
    assert max(h["hop"] for h in hops) == 2
    deny_hop = next(h for h in hops if h["hop"] == 2 and h["stage"] == "policy")
    assert deny_hop["verdict"] == "deny"
    first_connect = next(h for h in hops if h["hop"] == 1 and h["stage"] == "connect")
    assert first_connect["location"] == "http://metadata.example/latest/"


def test_redirect_allow_then_rebind(factory):
    kernel, connector = factory(_redirect_routes())
    result = kernel.fetch("http://127.0.0.1:18080/redirect-rebind")
    assert result["final_verdict"] == "deny"
    assert "127.0.0.1" in connector.attempted_peers()
    assert not {"203.0.113.7", "169.254.169.254"} & set(connector.attempted_peers())


def test_redirect_allow_then_mapped_ipv6(factory):
    kernel, connector = factory(_redirect_routes())
    result = kernel.fetch("http://127.0.0.1:18080/redirect-mapped")
    assert result["final_verdict"] == "deny"
    hops = result["hops"]
    deny = next(h for h in hops if h["stage"] == "policy" and h["verdict"] == "deny")
    # 解包后按环回规则拒绝
    assert deny["matched"]["rule_id"] == "deny-loopback-v4"
    assert "127.0.0.1" in deny["resolved"]


def test_redirect_loop_is_state_conflict(factory):
    kernel, connector = factory(_redirect_routes())
    result = kernel.fetch("http://127.0.0.1:18080/loop-a")
    assert result["final_verdict"] == "state_conflict"
    assert result["error"]["code"] == "E_REDIRECT_LOOP"
    cycle = result["error"]["details"]["cycle"]
    # 环：A -> B -> A
    assert cycle[0] == cycle[-1]
    assert len(cycle) == 3


def test_redirect_budget_is_resource_exhausted(factory):
    kernel, connector = factory(_redirect_routes())
    result = kernel.fetch("http://127.0.0.1:18080/many")
    assert result["final_verdict"] == "resource_exhausted"
    assert result["error"]["code"] == "E_REDIRECT_BUDGET"
    # 与重定向环区分：错误类别不同
    assert result["error"]["category"] == "resource_exhausted"
    # 预算内允许的对端确实被连（max_redirects=2 → 初始+2 共 3 次请求上限）
    assert connector.attempted_peers(), "预算耗尽前应有真实连接"
    assert len(connector.attempts) == 3


def test_loop_and_budget_categories_distinct(factory):
    """状态冲突与资源耗尽必须是不同失败类别（可区分性要求）。"""
    kernel, _ = factory(_redirect_routes())
    loop = kernel.fetch("http://127.0.0.1:18080/loop-a")
    budget = kernel.fetch("http://127.0.0.1:18080/many")
    assert loop["error"]["category"] != budget["error"]["category"]
    assert {"state_conflict", "resource_exhausted"} == {
        loop["error"]["category"], budget["error"]["category"]
    }
