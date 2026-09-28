#!/usr/bin/env python3
"""端到端演示：启动仅绑定本机的测试服务，通过安全内核访问它，
并展示四类攻击夹具（重绑定 / mapped IPv6 / userinfo / 重定向环）的决策链。

用法::

    python -m scripts.run_demo                # 内核直调 + 决策链 JSON
    python -m scripts.run_demo --web          # 额外启动 FastAPI，打印 curl 示例
    python -m scripts.run_demo --https        # 同时演示本地 TLS（合成 CA）

全程不访问公网：所有 DNS 应答来自夹具，所有连接目标为 127.0.0.1。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.audit import AuditStore  # noqa: E402
from app.demo_upstream import DemoUpstream  # noqa: E402
from app.kernel import GuardKernel  # noqa: E402
from app.pki import client_ssl_context, ensure_demo_pki, server_ssl_context  # noqa: E402
from app.policy import Policy  # noqa: E402
from app.resolver import ControlledResolver  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
STATE = Path(os.environ.get("SSRF_GUARD_STATE", ROOT / "state" / "demo"))


def _grant(host: str, scheme: str, port: int, *, tag_any=None, records=("127.0.0.1",), rid=None):
    g = {
        "id": rid or f"grant-{host}-{port}",
        "action": "allow",
        "host": host,
        "scheme": scheme,
        "port": port,
        "tag_any": tag_any or [],
        "note": f"demo per-run grant -> 127.0.0.1:{port}",
        "records": list(records),
    }
    return g


def _print_chain(title: str, result) -> None:
    data = result.to_dict()
    line = "=" * 78
    print("\n" + line)
    print(f"# {title}")
    print(line)
    print(f"run_id      : {data['run_id']}")
    print(f"request     : {data['requested_url']}")
    print(f"verdict     : {data['verdict']}  status={data['status']}  "
          f"hops={len(data['hops'])}  duration_ms={data['duration_ms']}")
    if data["failure"]:
        f = data["failure"]
        print(f"failure     : [{f['kind']}] {f['reason']} -- {f['message']}")
    print("-" * 78)
    for ev in data["decision_chain"]:
        v = f"[{ev['verdict']}]" if ev["verdict"] else "[*]     "
        reason = f" {ev['reason']}" if ev["reason"] else ""
        detail = ev["detail"]
        keep = {}
        for k in ("hop", "normalized_url", "host", "host_kind", "source",
                  "addresses", "pinned_ip", "peer_ip", "status", "to",
                  "decision", "from"):
            if k in detail:
                keep[k] = detail[k]
        print(f"{ev['seq']:02d} {v:9} {ev['stage']:14}{reason}")
        if keep:
            print("    " + json.dumps(keep, ensure_ascii=False, sort_keys=True)[:500])
    if data["response"]:
        print("-" * 78)
        print("response:", json.dumps(
            {k: data["response"][k] for k in ("status", "body_bytes", "peer_ip", "body_preview")
             if k in data["response"]}, ensure_ascii=False)[:500])
    # 连接尝试汇总：被拒目标必须为零连接
    attempts = [a for h in data["hops"] for a in h["attempts"]]
    opened = [a for a in attempts if a["result"] in ("opened", "completed")]
    print(f"connections : {len(opened)} opened -> {[a['ip'] for a in opened]}")


def build_stack(*, use_https: bool, state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    pki = ensure_demo_pki(state_dir) if use_https else None
    server_ctx = server_ssl_context(pki) if use_https else None
    upstream = DemoUpstream(
        hostname="demo.local", scheme="https" if use_https else "http",
        port=0, ssl_context=server_ctx,
    ).start()
    # 等服务器就绪
    time.sleep(0.1)
    tls_ctx = client_ssl_context(pki.ca_cert_path) if use_https else None
    resolver = ControlledResolver.from_file(FIXTURES / "dns" / "zones.json")
    policy = Policy.from_file(FIXTURES / "policy" / "rules.json")
    audit = AuditStore(state_dir / "audit")
    return upstream, resolver, policy, audit, tls_ctx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", action="store_true", help="同时启动 FastAPI 服务")
    ap.add_argument("--https", action="store_true", help="演示上游启用本地合成 TLS")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--web-port", type=int, default=8088)
    args = ap.parse_args()

    STATE.mkdir(parents=True, exist_ok=True)
    upstream, resolver, base_policy, audit, tls_ctx = build_stack(
        use_https=args.https, state_dir=STATE
    )
    scheme = upstream.scheme
    base = upstream.base_url()
    port = upstream.port
    print(f"演示上游已启动（仅 127.0.0.1）: {base}")
    print(f"审计状态目录: {STATE / 'audit'}")

    scenarios: list[tuple[str, str, dict | None]] = [
        ("允许：精确 grant 访问本机 /ok", f"{base}/ok",
         {"grants": [_grant("demo.local", scheme, port, records=["127.0.0.1"])]}),
        ("重定向：/redirect -> /ok，每跳重新校验", f"{base}/redirect?to=/ok",
         {"grants": [_grant("demo.local", scheme, port)]}),
        ("攻击：重定向跳到云元数据 169.254.169.254（禁止、零连接）",
         f"{base}/redirect?to=http://169.254.169.254/latest/meta-data/",
         {"grants": [_grant("demo.local", scheme, port)]}),
        ("攻击：重定向环 /loop-a <-> /loop-b（state_conflict）",
         f"{base}/loop-a",
         {"grants": [_grant("demo.local", scheme, port)], "max_redirects": 5}),
        ("攻击：跳数耗尽 /chain?n=9（resource_exhausted）",
         f"{base}/chain?n=9",
         {"grants": [_grant("demo.local", scheme, port)], "max_redirects": 5}),
        ("攻击：跳转到 file:// 方案（input_error）",
         f"{base}/file-redirect",
         {"grants": [_grant("demo.local", scheme, port)]}),
        ("攻击：DNS 重绑定首帧即元数据（policy_denied、零连接）",
         "http://rebind-meta-then-public.test/", None),
        ("攻击：mapped IPv6 元数据 [::ffff:169.254.169.254]",
         "http://[::ffff:169.254.169.254]/", None),
        ("攻击：URL userinfo 片段（input_error）",
         "http://user:pw@demo.local/ok", None),
        ("攻击：整数 IP 2130706433（input_error）",
         "http://2130706433/", None),
        ("解析失败：NXDOMAIN 夹具（computation_failed）",
         "http://nxdomain.test/", None),
        ("资源耗尽：超大响应体（resource_exhausted）",
         f"{base}/large?bytes=2000000",
         {"grants": [_grant("demo.local", scheme, port)], "max_bytes": 4096}),
    ]

    for title, url, opts in scenarios:
        opts = opts or {}
        grants = opts.get("grants")
        resolver_clone = resolver.clone()
        # grant 中的 records 是演示便利字段：注入到一次性 resolver 克隆
        rule_grants = []
        for g in grants or []:
            if "records" in g:
                resolver_clone.add_zone(g["host"], {"records": list(g["records"])})
            rule_grants.append({k: v for k, v in g.items() if k != "records"})
        policy = base_policy.with_grants(rule_grants) if rule_grants else base_policy
        kernel = GuardKernel(
            resolver=resolver_clone,
            policy=policy,
            max_redirects=opts.get("max_redirects", 5),
            max_bytes=opts.get("max_bytes", 1 << 20),
            timeout_s=5.0,
            tls_context=tls_ctx,
        )
        result = kernel.fetch(url, audit_sink=audit)
        _print_chain(title, result)

    print("\n" + "=" * 78)
    print("审计 runs:", json.dumps(audit.list_runs(limit=3), ensure_ascii=False))
    print(f"事件流文件: {STATE / 'audit' / 'events.jsonl'}")

    if args.web:
        import uvicorn

        from app.webapi import KernelFactory, create_app

        # web 模式把 demo.local 的端口记录预置进 resolver
        resolver.add_zone("demo.local", {"records": ["127.0.0.1"]})
        factory = KernelFactory(
            resolver=resolver,
            base_policy=base_policy,
            audit=audit,
            tls_context=tls_ctx,
            defaults={"max_redirects": 5, "max_bytes": 1 << 20, "timeout_s": 5.0},
        )
        app = create_app(kernel_factory=factory)
        print("\nFastAPI 已启动，示例调用：")
        print(f"  curl -s http://{args.host}:{args.web_port}/health")
        print(f"  curl -s -XPOST http://{args.host}:{args.web_port}/v1/fetch \\")
        print(f"    -H 'content-type: application/json' \\")
        print(f"    -d '{{\"url\":\"{base}/ok\",\"grants\":["
              f"{{\"host\":\"demo.local\",\"scheme\":\"{scheme}\",\"port\":{port},"
              f"\"records\":[\"127.0.0.1\"]}}]}}' | python -m json.tool")
        print("按 Ctrl+C 退出。")
        try:
            uvicorn.run(app, host=args.host, port=args.web_port, log_level="warning")
        finally:
            upstream.stop()
    else:
        upstream.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
