"""本地合成 TLS：通过临时 CA 校验服务证书，TCP 仍固定到 127.0.0.1。"""
from __future__ import annotations

from app.contracts import FailureKind, Reason, Stage, Verdict


def test_https_demo_ok_with_local_ca(make_kernel, demo_https):
    server, client_ctx = demo_https
    kernel = make_kernel(
        grants=[{
            "id": "g-demo-https",
            "host": "demo.local",
            "scheme": "https",
            "port": server.port,
            "records": ["127.0.0.1"],
        }],
        tls_context=client_ctx,
    )
    result = kernel.fetch(f"https://demo.local:{server.port}/ok")
    assert result.verdict == Verdict.ALLOW.value, result.failure
    assert result.response["status"] == 200
    connect = [e for e in result.evidence if e.stage == Stage.CONNECT.value][0]
    assert connect.detail["transport"] == "tls"
    assert connect.detail["peer_ip"] == "127.0.0.1"


def test_https_without_trusted_ca_is_computation_failed(make_kernel, demo_https):
    import ssl

    server, _ = demo_https
    untrusting = ssl.create_default_context()  # 不含演示 CA -> 证书链无法验证
    kernel = make_kernel(
        grants=[{
            "id": "g-demo-https", "host": "demo.local", "scheme": "https",
            "port": server.port, "records": ["127.0.0.1"],
        }],
        tls_context=untrusting,
    )
    result = kernel.fetch(f"https://demo.local:{server.port}/ok")
    assert result.failure["kind"] == FailureKind.COMPUTATION_FAILED.value
    assert result.failure["reason"] == Reason.TLS_CERT_VERIFY.value
