"""pytest 共享夹具：本机演示上游、受控解析器、策略、内核工厂、审计目录。"""
from __future__ import annotations

import json
import socket
import ssl
from pathlib import Path

import pytest

from app.audit import AuditStore
from app.connector import PinnedConnector
from app.demo_upstream import DemoUpstream
from app.kernel import GuardKernel
from app.pki import client_ssl_context, ensure_demo_pki, server_ssl_context
from app.policy import Policy
from app.resolver import ControlledResolver

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


@pytest.fixture(scope="session")
def demo_http():
    server = DemoUpstream(hostname="demo.local", scheme="http", port=0).start()
    # 轮询直到端口可连
    _wait_port("127.0.0.1", server.port)
    yield server
    server.stop()


@pytest.fixture(scope="session")
def demo_https(tmp_path_factory):
    state = tmp_path_factory.mktemp("pki")
    pki = ensure_demo_pki(state)
    server = DemoUpstream(
        hostname="demo.local", scheme="https", port=0,
        ssl_context=server_ssl_context(pki),
    ).start()
    _wait_port("127.0.0.1", server.port)
    client_ctx = client_ssl_context(pki.ca_cert_path)
    yield server, client_ctx
    server.stop()


def _wait_port(host: str, port: int, timeout: float = 3.0) -> None:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.02)
    raise RuntimeError(f"demo server on {host}:{port} did not start")


@pytest.fixture()
def zones_resolver():
    """每个测试一个全新解析器（脚本计数隔离）。"""
    return ControlledResolver.from_file(FIXTURES / "dns" / "zones.json")


@pytest.fixture()
def base_policy():
    return Policy.from_file(FIXTURES / "policy" / "rules.json")


@pytest.fixture()
def audit_store(tmp_path):
    store = AuditStore(tmp_path / "audit")
    yield store
    store.close()


@pytest.fixture()
def make_kernel(zones_resolver, base_policy):
    """工厂：(grants=None, *, resolver=None, overrides=..., max_bytes=...) -> kernel。"""

    def _make(grants=None, *, resolver=None, overrides=None, max_redirects=5,
              max_bytes=1 << 20, timeout_s=5.0, tls_context=None, connector=None):
        res = resolver or zones_resolver.clone(overrides)
        # grants 中的 records 注入到解析器
        grant_rules = []
        for g in grants or []:
            if "records" in g:
                res.add_zone(g["host"], {"records": list(g["records"])})
            grant_rules.append(
                {"action": "allow", **{k: v for k, v in g.items() if k != "records"}}
            )
        policy = base_policy.with_grants(grant_rules)
        return GuardKernel(
            resolver=res,
            policy=policy,
            connector=connector,
            max_redirects=max_redirects,
            max_bytes=max_bytes,
            timeout_s=timeout_s,
            tls_context=tls_context,
        )

    return _make


@pytest.fixture()
def demo_grant(demo_http):
    """精确放行 demo http 上游的 grant（host+scheme+port）。"""
    return [{
        "id": "g-demo",
        "host": "demo.local",
        "scheme": "http",
        "port": demo_http.port,
        "records": ["127.0.0.1"],
    }]


@pytest.fixture()
def oracle():
    return json.loads((FIXTURES / "expected" / "expected_results.json").read_text("utf-8"))


def find_case(oracle_data, case_id: str) -> dict:
    for case in oracle_data["cases"]:
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


@pytest.fixture()
def closed_port():
    """返回一个当前没有监听的 TCP 端口（尽力而为：绑定后立即关闭）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
