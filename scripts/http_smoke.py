"""实时服务冒烟：对运行中的 uvicorn 发起签名提交与差分（仅标准库 urllib）。

前置：服务已在 127.0.0.1:8088 启动（verify.sh 负责）。
"""

from __future__ import annotations

import json
import urllib.request

from diffanalyzer.config import load_config
from diffanalyzer.crypto_verify import load_private_key
from diffanalyzer.local_signing import signed_policy_envelope

BASE = "http://127.0.0.1:8088"


def call(method: str, path: str, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read())


def main() -> None:
    cfg = load_config()
    priv = load_private_key(cfg.abs_path(cfg.demo_private_key_path).read_bytes())

    old = {"version": "smoke-v1", "rules": [
        {"id": "r", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["get"], "principals": ["acct/alice"]}]}
    new = {"version": "smoke-v2", "rules": [
        {"id": "r", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["get"], "principals": ["acct/alice"]},
        {"id": "r2", "effect": "ALLOW", "resource_prefix": "logs/audit/",
         "actions": ["get"], "principals": ["*"], "anonymous": True}]}

    h = {"X-Actor": "smoke"}
    s1, b1 = call("POST", "/v1/policies",
                  signed_policy_envelope(
                      cfg.abs_path(cfg.demo_private_key_path).read_bytes(), old), h)
    s2, b2 = call("POST", "/v1/policies",
                  signed_policy_envelope(
                      cfg.abs_path(cfg.demo_private_key_path).read_bytes(), new), h)
    assert s1 == s2 == 200, (s1, s2)

    s3, body = call("POST", "/v1/diffs", {
        "old_version": "smoke-v1", "new_version": "smoke-v2",
        "scope": {"resource_prefixes": ["logs/"], "actions": ["get"]}}, h)
    assert s3 == 200
    assert body["summary"]["verdict"] == "WIDENED", body["summary"]
    witnesses = body["witnesses"]["widened"]
    assert witnesses, "必须返回新增允许见证"
    w0 = witnesses[0]["request"]
    assert w0["resource"].startswith("logs/audit/"), w0
    assert w0["principal"] in ("@other", None, "acct/bob"), w0
    print("HTTP 冒烟通过：新增允许见证 =", json.dumps(w0, ensure_ascii=False))

    # 失败类别冒烟：坏签名必须被明确拒绝
    bad = signed_policy_envelope(
        cfg.abs_path(cfg.demo_private_key_path).read_bytes(),
        {"version": "smoke-v3", "rules": []})
    bad["signature"] = "AAAA" + bad["signature"][4:]
    try:
        call("POST", "/v1/policies", bad, h)
        raise AssertionError("坏签名应被拒绝")
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read())
        assert e.code == 422 and \
            payload["failure"]["kind"] == "CRYPTO_BAD_SIGNATURE", payload
        print("HTTP 冒烟通过：坏签名被拒绝（CRYPTO_BAD_SIGNATURE, 422）")


if __name__ == "__main__":
    main()
