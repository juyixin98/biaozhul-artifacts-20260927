"""固定地址连接器（pinning）测试。

证明：
* PinnedHTTPConnector 收到的是 IP 字面量，连接目标 socket 层不再解析名字；
* 连接对端与已批准 pin 一致才放行（PinMismatchError 纵深防御）；
* 真实 HTTP 交换能从本机测试源站取到 200；
* 尝试连接一个被禁 IP（绕过内核直调连接器）会真实失败或被对端复核挡下，
  从而证明"连接地址"这一环本身就是字面量 IP 而非主机名。
"""

from __future__ import annotations

import socket

import pytest

from conftest import ORIGIN_PORT
from safeproxy.contracts import HostKind, IpCandidate, ParsedUrl
from safeproxy.errors import ConnectError, PinMismatchError
from safeproxy.net.connector import PinnedHTTPConnector
from safeproxy.net.urlparse import parse_and_normalize
from safeproxy.service.origin import OriginServer


@pytest.fixture(scope="module")
def origin():
    with OriginServer(ORIGIN_PORT) as s:
        yield s


def _parsed(url, schemes=frozenset({"http"})):
    return parse_and_normalize(url, schemes)


def test_real_connect_to_allowed_local_origin(origin):
    conn = PinnedHTTPConnector()
    parsed = _parsed(f"http://127.0.0.1:{ORIGIN_PORT}/ok")
    pin = IpCandidate(literal="127.0.0.1", family=socket.AF_INET, source="literal")
    resp = conn.fetch(parsed, pin, timeout=3)
    assert resp.status_code == 200
    assert b"origin-ok" in resp.body
    assert resp.peer[0] == "127.0.0.1"


def test_real_connect_follows_redirect_to_ok(origin):
    conn = PinnedHTTPConnector()
    parsed = _parsed(f"http://127.0.0.1:{ORIGIN_PORT}/redirect-ok")
    pin = IpCandidate(literal="127.0.0.1", family=socket.AF_INET)
    resp = conn.fetch(parsed, pin, timeout=3)
    assert resp.status_code == 302
    assert resp.location.endswith("/ok")


def test_connect_uses_ip_literal_not_name(origin, monkeypatch):
    """socket.create_connection/connect 收到的必须是 IP 字面量。

    拦截 socket.socket.connect，断言其参数主机是已批准的 pin IP，
    绝不可以是需要再次解析的域名。
    """
    seen = {}
    orig_connect = socket.socket.connect

    def spy_connect(self, address):
        seen["address"] = address
        return orig_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", spy_connect)
    conn = PinnedHTTPConnector()
    # 即便 parsed.host 是名字，传入连接器的 pin 也必须是字面量
    parsed = _parsed(f"http://local-origin.test:{ORIGIN_PORT}/ok")
    parsed = ParsedUrl(
        url=parsed.url, scheme=parsed.scheme, host="local-origin.test",
        host_kind=HostKind.DOMAIN, port=ORIGIN_PORT, has_userinfo=False,
        userinfo_witness="", path="/ok", query="", fragment="",
    )
    pin = IpCandidate(literal="127.0.0.1", family=socket.AF_INET)
    resp = conn.fetch(parsed, pin, timeout=3)
    assert resp.status_code == 200
    assert seen["address"][0] == "127.0.0.1", (
        f"连接器必须连字面量 IP，实际连接目标={seen['address'][0]!r}"
    )


def test_connection_to_closed_port_is_computation_failed(origin):
    """连接失败归类为 computation_failed/connect，而非策略问题。"""
    conn = PinnedHTTPConnector()
    parsed = _parsed("http://127.0.0.1:9/")  # discard 端口，几乎必然拒连
    pin = IpCandidate(literal="127.0.0.1", family=socket.AF_INET)
    with pytest.raises(ConnectError) as ei:
        conn.fetch(parsed, pin, timeout=2)
    assert ei.value.category.value == "computation_failed"


def test_peer_mismatch_detected(origin, monkeypatch):
    """对端复核：若实际对端不等于 pin，抛 PinMismatchError（纵深防御）。"""
    # 让 getpeername 谎报一个被禁地址，验证复核逻辑会拦下
    orig_peername = socket.socket.getpeername

    def fake_peername(self):
        return ("169.254.169.254", 12345)

    monkeypatch.setattr(socket.socket, "getpeername", fake_peername)
    conn = PinnedHTTPConnector()
    parsed = _parsed(f"http://127.0.0.1:{ORIGIN_PORT}/ok")
    pin = IpCandidate(literal="127.0.0.1", family=socket.AF_INET)
    with pytest.raises(PinMismatchError) as ei:
        conn.fetch(parsed, pin, timeout=3)
    assert ei.value.code == "E_PIN_MISMATCH"
    assert ei.value.details["peer"] == "169.254.169.254"
    # 恢复，避免影响后续
    monkeypatch.setattr(socket.socket, "getpeername", orig_peername)
