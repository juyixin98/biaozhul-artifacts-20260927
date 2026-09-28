"""测试夹具：内核组装 + 记录型连接器。

核心设计：``RecordingConnector`` 记录每一次 ``connect((host, port))`` 的对端。
策略拒绝发生在 connect 之前，因此测试可以严格断言：被禁 IP **从未**
出现在连接尝试记录里——这是"禁止地址从未被连接"的直接证据。

重定向用内存响应模拟（不依赖真实 socket），让重绑定/重定向环/预算测试
既确定又快；端到端真实连接单独在 test_e2e_real_connect.py 用本机源站验证。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from safeproxy.contracts import IpCandidate, ParsedUrl  # noqa: E402
from safeproxy.errors import ConnectError  # noqa: E402
from safeproxy.kernel import SecurityKernel  # noqa: E402
from safeproxy.net.connector import RawResponse  # noqa: E402
from safeproxy.net.resolver import FixtureResolver, parse_zone  # noqa: E402
from safeproxy.rules.loader import load_policy  # noqa: E402
from safeproxy.rules.policy import PolicyEngine  # noqa: E402

POLICY_PATH = ROOT / "fixtures" / "policy.json"
ZONE_PATH = ROOT / "fixtures" / "dns" / "primary.zone"
EXPECTED_PATH = ROOT / "fixtures" / "expected.json"

ORIGIN_PORT = 18080


class RecordingConnector:
    """假连接器：不发真实网络，按路由表返回预制响应；记录所有连接对端。"""

    def __init__(self, routes: dict[str, RawResponse] | None = None, *, fail_hosts=()):
        self.routes = routes or {}
        self.fail_hosts = set(fail_hosts)
        self.attempts: list[tuple[str, int]] = []
        self.attempted_pins: list[str] = []

    def fetch(self, parsed: ParsedUrl, pin: IpCandidate, **kwargs) -> RawResponse:
        # 忠实记录：内核到底想连哪个 IP:port
        self.attempts.append((pin.literal, parsed.port))
        self.attempted_pins.append(pin.literal)

        if pin.literal in self.fail_hosts or parsed.host in self.fail_hosts:
            raise ConnectError(
                f"测试夹具：拒绝连接 {pin.literal}",
                details={"pin": pin.literal, "port": parsed.port},
            )

        # 路由键：host:port + path（优先），其次仅 path（用于字面量/名字同源）
        path_key = f"{parsed.path}"
        host_key = f"{parsed.host}:{parsed.port}{parsed.path}"
        if host_key in self.routes:
            return self._with_peer(self.routes[host_key], pin)
        if path_key in self.routes:
            return self._with_peer(self.routes[path_key], pin)
        raise ConnectError(
            f"测试夹具中没有 {host_key} 的路由",
            details={"pin": pin.literal, "path": parsed.path},
        )

    @staticmethod
    def _with_peer(resp: RawResponse, pin: IpCandidate) -> RawResponse:
        return RawResponse(
            status_code=resp.status_code,
            location=resp.location,
            headers=dict(resp.headers),
            body=resp.body,
            peer=(pin.literal, 0),
        )

    def attempted_peers(self) -> list[str]:
        return [host for host, _port in self.attempts]


def redirect(location: str) -> RawResponse:
    return RawResponse(302, location, {"location": location, "content-length": "0"}, b"", ("", 0))


def ok(body: bytes = b"hello") -> RawResponse:
    return RawResponse(200, None, {"content-length": str(len(body))}, body, ("", 0))


@pytest.fixture(scope="session")
def bundle():
    return load_policy(str(POLICY_PATH))


@pytest.fixture(scope="session")
def zone_table():
    return parse_zone(str(ZONE_PATH))


@pytest.fixture
def expected_cases():
    with open(EXPECTED_PATH, encoding="utf-8") as fh:
        return json.load(fh)["cases"]


def make_kernel(bundle, table, connector: RecordingConnector, *, audit=None,
                run_ids=None) -> tuple[SecurityKernel, RecordingConnector]:
    engine = PolicyEngine(bundle)
    resolver = FixtureResolver(dict(table))
    id_factory = (lambda: next(iter(run_ids))) if run_ids else None
    kernel = SecurityKernel(
        engine, resolver, connector, audit=audit,
        **({"run_id_factory": id_factory} if run_ids else {}),
    )
    return kernel, connector


@pytest.fixture
def factory(bundle, zone_table):
    """返回一个 (connector_routes, fail_hosts) -> (kernel, connector) 工厂。"""

    def _factory(routes=None, fail_hosts=(), audit=None):
        connector = RecordingConnector(routes, fail_hosts=fail_hosts)
        engine = PolicyEngine(bundle)
        resolver = FixtureResolver(dict(zone_table))
        kernel = SecurityKernel(engine, resolver, connector, audit=audit)
        return kernel, connector

    return _factory
