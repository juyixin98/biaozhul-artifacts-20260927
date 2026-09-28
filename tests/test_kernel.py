"""安全内核集成测试：重绑定 / mapped IPv6 / userinfo / 重定向环等。

关键断言不是“接口能调用”，而是：
* 最终 verdict / status；
* 失败类别 kind 与细粒度 reason；
* **被禁地址的实际连接尝试为 0**（RecordingConnector 逐条记录）；
* pinning IP 与对端 IP；
* 决策链包含完整阶段与每跳重校验。
"""
from __future__ import annotations

import json
import socket
import ssl
from pathlib import Path

import pytest

from app.connector import PinnedConnector
from app.contracts import FailureKind, Reason, Stage, Verdict


# --------------------------------------------------------------------------
# 1) DNS rebinding
# --------------------------------------------------------------------------

def test_rebind_meta_first_denied_without_any_connection(make_kernel):
    kernel = make_kernel()
    result = kernel.fetch("http://rebind-meta-then-public.test/")
    assert result.verdict == "deny"
    assert result.failure["kind"] == FailureKind.POLICY_DENIED.value
    assert result.failure["reason"] == Reason.IP_BLOCKED.value
    denied = _policy_decision(result)["addresses"]
    assert denied[0]["ip"] == "169.254.169.254"
    assert "link-local" in denied[0]["tags"]
    assert _all_connections(result) == []  # 元数据地址从未被连接
    assert _stage_reasons(result, Stage.POLICY) == [Reason.IP_BLOCKED.value]


def test_rebind_second_frame_bad_uses_fresh_snapshot_no_reuse(make_kernel):
    # 第一帧 203.0.113.10(documentation)：tag grant 放行；用假连接器避免真实外联，
    # 同时断言内核固定在快照 IP，且只解析一次。
    logged = []

    class FakeConn:
        transport = "tcp"
        peer_ip = "203.0.113.10"
        class sock:
            @staticmethod
            def sendall(b): logged.append(("sent", b[:4]))
            @staticmethod
            def close(): pass

    class FakeConnector:
        def connect(self, target, chosen, *, timeout, tls_context=None):
            assert chosen.ip == "203.0.113.10"  # 固定在第一次解析结果
            return FakeConn()

    kernel1 = make_kernel(
        grants=[{"id": "g-rebind", "host": "rebind-public-then-meta.test",
                 "scheme": "http", "port": 80, "tag_any": ["documentation"]}],
        connector=FakeConnector(),
    )
    # 假连接器后 httpclient 读取会失败 -> computation_failed，但策略/pin 断言已成立
    r1 = kernel1.fetch("http://rebind-public-then-meta.test/")
    # 走到 connect 之后才在读阶段失败，证明策略允许且固定连接 203.0.113.10
    assert any(e.stage == Stage.CONNECT.value and
               e.detail.get("pinned_ip") == "203.0.113.10" for e in r1.evidence)
    # 每跳只解析一次
    dns_events = [e for e in r1.evidence if e.stage == Stage.DNS_RESOLVE.value]
    assert len(dns_events) == 1

    # 同一 resolver 推进到第二帧 -> 元数据，拒绝且零连接
    resolver2 = kernel1.resolver
    kernel2 = make_kernel(
        resolver=resolver2,
        grants=[{"id": "g-rebind", "host": "rebind-public-then-meta.test",
                 "scheme": "http", "port": 80, "tag_any": ["documentation"]}],
        connector=FakeConnector(),
    )
    r2 = kernel2.fetch("http://rebind-public-then-meta.test/")
    assert r2.verdict == "deny"
    assert r2.failure["reason"] == Reason.IP_BLOCKED.value
    assert _all_connections(r2) == []


# --------------------------------------------------------------------------
# 2) 混合允许/禁止地址集合
# --------------------------------------------------------------------------

def test_mixed_candidate_set_whole_hop_denied(make_kernel):
    kernel = make_kernel(grants=[{
        "id": "g-doc-only", "host": "mixed-allow-deny.test",
        "scheme": "http", "port": 80, "tag_any": ["documentation"],
    }])
    result = kernel.fetch("http://mixed-allow-deny.test/")
    assert result.failure["reason"] == Reason.MIXED_CANDIDATES.value
    decision = _policy_decision(result)
    by_ip = {a["ip"]: a["action"] for a in decision["addresses"]}
    assert by_ip == {"203.0.113.10": "allow", "10.0.0.5": "deny"}
    assert _all_connections(result) == []  # 即使部分允许也不连接任何候选


def test_multi_meta_denied_no_connection(make_kernel):
    kernel = make_kernel()
    result = kernel.fetch("http://multi-meta.test/")
    assert result.failure["reason"] == Reason.IP_BLOCKED.value
    denied = _policy_decision(result)["addresses"]
    assert {a["ip"] for a in denied} == {"169.254.169.254", "169.254.170.2"}
    assert _all_connections(result) == []


# --------------------------------------------------------------------------
# 3) IPv4-mapped IPv6
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url,canonical,unwrapped", [
    ("http://[::ffff:169.254.169.254]/", "169.254.169.254", True),
    ("http://mapped-meta.test/", "169.254.169.254", True),
])
def test_mapped_ipv6_metadata_unwrapped_denied(make_kernel, url, canonical, unwrapped):
    kernel = make_kernel()
    result = kernel.fetch(url)
    assert result.failure["reason"] == Reason.IP_BLOCKED.value
    addrs = _policy_decision(result)["addresses"]
    assert addrs[0]["ip"] == canonical
    assert addrs[0]["family"] == "ipv4"
    if unwrapped:
        assert addrs[0]["unwrapped_from"]
    assert "link-local" in addrs[0]["tags"]
    assert _all_connections(result) == []


def test_mapped_ipv6_public_equivalent_classification(make_kernel):
    kernel = make_kernel()
    result = kernel.fetch("http://mapped-public.test/")
    addrs = _policy_decision(result)["addresses"]
    assert addrs[0]["ip"] == "203.0.113.10"
    assert addrs[0]["family"] == "ipv4"
    assert addrs[0]["unwrapped_from"]
    assert "documentation" in addrs[0]["tags"]
    assert result.failure["reason"] == Reason.IP_BLOCKED.value  # 静态 documentation deny


def test_ipv6_ula_denied(make_kernel):
    kernel = make_kernel()
    result = kernel.fetch("http://ipv6-ula.test/")
    addrs = _policy_decision(result)["addresses"]
    assert addrs[0]["ip"] == "fd00::1"
    assert "unique-local" in addrs[0]["tags"]
    assert _all_connections(result) == []


# --------------------------------------------------------------------------
# 4) userinfo / 主机混淆
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url,reason", [
    ("http://user:pw@demo.local/ok", Reason.USERINFO_FORBIDDEN),
    ("http://169.254.169.254@demo.local/ok", Reason.USERINFO_FORBIDDEN),
    ("http://2130706433/latest/", Reason.HOST_INTEGER_IP),
    ("http://0177.0.0.1/", Reason.HOST_AMBIGUOUS_NUMERIC),
    ("file:///etc/passwd", Reason.SCHEME_UNSUPPORTED),
    ("http://127.0.0.1\\@evil.com/", Reason.URL_BAD_CHARACTER),
])
def test_input_confusions_denied_pre_dns(make_kernel, url, reason):
    kernel = make_kernel()
    result = kernel.fetch(url)
    assert result.failure["kind"] == FailureKind.INPUT_ERROR.value
    assert result.failure["reason"] == reason.value
    assert _all_connections(result) == []
    # 输入错误不得触发 DNS
    assert not [e for e in result.evidence if e.stage == Stage.DNS_RESOLVE.value]


# --------------------------------------------------------------------------
# 5) 重定向：每跳重校验 / 环 / 跳数 / 方案降级
# --------------------------------------------------------------------------

def test_demo_ok_end_to_end_pinned_to_loopback(make_kernel, demo_http, demo_grant):
    kernel = make_kernel(grants=demo_grant)
    result = kernel.fetch(f"http://demo.local:{demo_http.port}/ok")
    assert result.verdict == Verdict.ALLOW.value
    assert result.response["status"] == 200
    conns = _all_connections(result)
    assert conns and all(a["ip"] == "127.0.0.1" for a in conns)
    connect_ev = [e for e in result.evidence if e.stage == Stage.CONNECT.value]
    assert connect_ev[0].detail["pinned_ip"] == "127.0.0.1"
    assert connect_ev[0].detail["peer_ip"] == "127.0.0.1"


def test_redirect_each_hop_revalidated(make_kernel, demo_http, demo_grant):
    kernel = make_kernel(grants=demo_grant)
    result = kernel.fetch(f"http://demo.local:{demo_http.port}/redirect?to=/who")
    assert result.verdict == Verdict.ALLOW.value
    assert len(result.hops) == 2
    # 每一跳都出现完整阶段序列
    for hop_index in (1, 2):
        stages = [e.stage for e in result.evidence if e.detail.get("hop") == hop_index]
        for required in ("url_parse", "dns_resolve", "ip_classify", "policy", "connect"):
            assert required in stages, (hop_index, stages)
    # 每跳 DNS 恰好一次
    assert len([e for e in result.evidence if e.stage == Stage.DNS_RESOLVE.value]) == 2


def test_redirect_loop_is_state_conflict(make_kernel, demo_http, demo_grant):
    kernel = make_kernel(grants=demo_grant)
    result = kernel.fetch(f"http://demo.local:{demo_http.port}/loop-a")
    assert result.failure["kind"] == FailureKind.STATE_CONFLICT.value
    assert result.failure["reason"] == Reason.REDIRECT_LOOP.value
    # 请求 1、2 成功，第 3 个目标在发现重复后直接拒绝（不连接）
    conns = _all_connections(result)
    assert len(conns) == 2
    assert all(a["ip"] == "127.0.0.1" for a in conns)
    repeated = result.failure["details"]["repeated_url"]
    assert repeated.endswith("/loop-a")


def test_redirect_limit_is_resource_exhausted(make_kernel, demo_http, demo_grant):
    kernel = make_kernel(grants=demo_grant, max_redirects=5)
    result = kernel.fetch(f"http://demo.local:{demo_http.port}/chain?n=9")
    assert result.failure["kind"] == FailureKind.RESOURCE_EXHAUSTED.value
    assert result.failure["reason"] == Reason.REDIRECT_LIMIT.value
    assert len(_all_connections(result)) == 6  # 6 个请求后中止
    assert len(result.hops) == 6


def test_redirect_to_file_scheme_input_error(make_kernel, demo_http, demo_grant):
    kernel = make_kernel(grants=demo_grant)
    result = kernel.fetch(f"http://demo.local:{demo_http.port}/file-redirect")
    assert result.failure["kind"] == FailureKind.INPUT_ERROR.value
    assert result.failure["reason"] == Reason.REDIRECT_SCHEME_UNSUPPORTED.value
    assert len(result.hops) == 1


def test_redirect_to_metadata_denied_at_hop_zero_connections_to_meta(
        make_kernel, demo_http, demo_grant):
    kernel = make_kernel(grants=demo_grant)
    result = kernel.fetch(
        f"http://demo.local:{demo_http.port}/redirect?to="
        f"http://169.254.169.254/latest/meta-data/"
    )
    assert result.failure["kind"] == FailureKind.POLICY_DENIED.value
    assert result.failure["reason"] == Reason.IP_BLOCKED.value
    conns = _all_connections(result)
    # 只有第一跳连了 127.0.0.1；元数据地址零连接
    assert [a["ip"] for a in conns] == ["127.0.0.1"]
    assert len(result.hops) == 2  # 第二跳完成了 parse+dns+classify+deny
    hop2_policy = [e for e in result.evidence
                   if e.stage == Stage.POLICY.value and e.detail.get("hop") == 2]
    assert hop2_policy[-1].verdict == "deny"


def test_redirect_to_rebind_host_denied_no_meta_connection(
        make_kernel, demo_http, demo_grant):
    kernel = make_kernel(grants=demo_grant)
    result = kernel.fetch(
        f"http://demo.local:{demo_http.port}/redirect?to="
        f"http://rebind-meta-then-public.test/"
    )
    assert result.failure["reason"] == Reason.IP_BLOCKED.value
    conns = _all_connections(result)
    assert [a["ip"] for a in conns] == ["127.0.0.1"]
    assert "169.254.169.254" not in [a["ip"] for a in conns]


# --------------------------------------------------------------------------
# 6) 失败类别区分：解析 / 连接 / 资源 / pin
# --------------------------------------------------------------------------

def test_nxdomain_is_computation_failed(make_kernel):
    kernel = make_kernel()
    result = kernel.fetch("http://nxdomain.test/")
    assert result.failure["kind"] == FailureKind.COMPUTATION_FAILED.value
    assert result.failure["reason"] == Reason.DNS_NAME_NOT_FOUND.value
    assert _all_connections(result) == []


def test_connection_refused_is_computation_failed(make_kernel, closed_port):
    kernel = make_kernel(grants=[{
        "id": "g-refused", "host": "refused.local", "scheme": "http",
        "port": closed_port, "records": ["127.0.0.1"],
    }])
    result = kernel.fetch(f"http://refused.local:{closed_port}/")
    assert result.failure["kind"] == FailureKind.COMPUTATION_FAILED.value
    assert result.failure["reason"] == Reason.CONNECT_REFUSED.value
    assert len(_all_connections(result)) == 1  # 尝试过，只是没人监听


def test_response_too_large_is_resource_exhausted(make_kernel, demo_http, demo_grant):
    kernel = make_kernel(grants=demo_grant, max_bytes=4096)
    result = kernel.fetch(f"http://demo.local:{demo_http.port}/large?bytes=2000000")
    assert result.failure["kind"] == FailureKind.RESOURCE_EXHAUSTED.value
    assert result.failure["reason"] == Reason.RESPONSE_TOO_LARGE.value


def test_pin_mismatch_detected_as_state_conflict(make_kernel, demo_http, demo_grant):
    import socket as _socket

    class LyingSocket(_socket.socket):
        """模拟透明代理/路由劫持：真实连接已建立，但 getpeername 报告其他地址。"""

        def getpeername(self):
            name = super().getpeername()
            return ("10.0.0.9", name[1]) + name[2:]

    class LyingConnector:
        def connect(self, target, chosen, *, timeout, tls_context=None):
            from app.connector import PinnedConnection

            raw = LyingSocket(_socket.AF_INET, _socket.SOCK_STREAM)
            raw.settimeout(timeout)
            raw.connect((chosen.ip, target.port))
            from app.connector import PinnedConnector

            peer = PinnedConnector._verified_peer(raw, chosen.ip, target.port)
            return PinnedConnection(sock=raw, peer_ip=peer, peer_port=target.port,
                                    tls=False, transport="tcp")

    kernel = make_kernel(grants=demo_grant, connector=LyingConnector())
    result = kernel.fetch(f"http://demo.local:{demo_http.port}/ok")
    assert result.failure["kind"] == FailureKind.STATE_CONFLICT.value
    assert result.failure["reason"] == Reason.PIN_MISMATCH.value
    assert result.failure["details"]["pinned_ip"] == "127.0.0.1"
    assert result.failure["details"]["peer_ip"] == "10.0.0.9"


# --------------------------------------------------------------------------
# 7) 决策链结构 & 审计
# --------------------------------------------------------------------------

def test_decision_chain_records_full_reasons(make_kernel, demo_http, demo_grant, audit_store):
    kernel = make_kernel(grants=demo_grant)
    result = kernel.fetch(
        f"http://demo.local:{demo_http.port}/redirect?to="
        f"http://169.254.169.254/x", audit_sink=audit_store
    )
    chain = [e.to_dict() for e in result.evidence]
    seqs = [e["seq"] for e in chain]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    stages = [e["stage"] for e in chain]
    assert stages[0] == "run_start"
    assert stages[-1] == "finish"
    # FINISH 上带最终裁决与原因
    finish = chain[-1]
    assert finish["verdict"] == "deny"
    assert finish["reason"] == Reason.IP_BLOCKED.value
    # 落盘可按 run_id 取回，且签名校验通过
    got = audit_store.get_run(result.run_id)
    assert got["run_id"] == result.run_id
    assert audit_store.verify_run(result.run_id)["valid"] is True


def test_audit_tamper_detection(audit_store, make_kernel, demo_http, demo_grant):
    kernel = make_kernel(grants=demo_grant)
    result = kernel.fetch(f"http://demo.local:{demo_http.port}/ok", audit_sink=audit_store)
    import sqlite3

    # 直接改库中的 canonical_json -> 签名必须失效
    conn = sqlite3.connect(audit_store.db_path)
    row = conn.execute("SELECT canonical_json FROM runs WHERE run_id=?",
                       (result.run_id,)).fetchone()
    tampered = row[0].replace('"verdict":"allow"', '"verdict":"deny"')
    conn.execute("UPDATE runs SET canonical_json=? WHERE run_id=?",
                 (tampered, result.run_id))
    conn.commit()
    conn.close()
    assert audit_store.verify_run(result.run_id)["valid"] is False


# --------------------------------------------------------------------------
# 8) 手写 oracle 交叉核对（抽样条目）
# --------------------------------------------------------------------------

def test_oracle_expected_results_match_behavior(make_kernel, demo_http, demo_grant, oracle):
    from tests.conftest import find_case

    case = find_case(oracle, "demo-ok-allowed-pinned")
    url = case["url_template"].replace("{port}", str(demo_http.port))
    grants = _instantiate(case["grants"], demo_http.port)
    result = make_kernel(grants=grants).fetch(url)
    exp = case["expect"]
    assert result.verdict == exp["verdict"]
    assert result.status == exp["status"]
    assert result.response["status"] == exp["http_status"]

    case2 = find_case(oracle, "redirect-loop-detected")
    grants2 = _instantiate(case2["grants"], demo_http.port)
    url2 = case2["url_template"].replace("{port}", str(demo_http.port))
    r2 = make_kernel(grants=grants2).fetch(url2)
    e2 = case2["expect"]
    assert r2.failure["kind"] == e2["failure_kind"]
    assert r2.failure["reason"] == e2["failure_reason"]
    assert len(_all_connections(r2)) == e2["connect_attempts"]

    case3 = find_case(oracle, "rebind-meta-first-never-connected")
    r3 = make_kernel().fetch(case3["url"])
    e3 = case3["expect"]
    assert r3.failure["kind"] == e3["failure_kind"]
    assert r3.failure["reason"] == e3["failure_reason"]
    assert _all_connections(r3) == []


def _instantiate(grants, port):
    out = []
    for g in grants:
        gg = dict(g)
        if gg.get("port") == "{port}":
            gg["port"] = port
        out.append(gg)
    return out  # records 字段由 make_kernel 工厂识别并注入 DNS


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------

def _all_connections(result):
    return [a for h in result.hops for a in h.attempts]


def _policy_decision(result):
    """从 policy_denied 失败详情取决策对象。"""
    assert result.failure and result.failure["kind"] == FailureKind.POLICY_DENIED.value
    return result.failure["details"]["decision"]


def _stage_reasons(result, stage):
    return [e.reason for e in result.evidence if e.stage == stage.value and e.reason]
