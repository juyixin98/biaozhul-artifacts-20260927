"""端到端演示：对本机测试服务运行正常与异常场景，输出可读决策链。

用法::

    python scripts/demo.py                 # 启动本机源站并跑全部场景
    python scripts/demo.py --jsonl         # 额外把结构化结果写到 artifacts/demo.jsonl

每个场景都通过真实安全内核（真实 PinnedHTTPConnector + 仅环回源站 + 受控
DNS 夹具），不访问任何公网地址。被禁地址在策略层即被拦截，连接器从不发起
到它们的连接。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from safeproxy.audit.store import AuditLog  # noqa: E402
from safeproxy.kernel import SecurityKernel  # noqa: E402
from safeproxy.net.connector import PinnedHTTPConnector  # noqa: E402
from safeproxy.net.resolver import FixtureResolver, parse_zone  # noqa: E402
from safeproxy.rules.loader import load_policy  # noqa: E402
from safeproxy.rules.policy import PolicyEngine  # noqa: E402
from safeproxy.service.origin import OriginServer  # noqa: E402

# (id, url, 说明)
SCENARIOS = [
    ("normal-ok", "http://127.0.0.1:18080/ok", "正常：允许的本机源站"),
    ("normal-redirect-ok", "http://127.0.0.1:18080/redirect-ok", "正常：同源重定向到 /ok"),
    ("attack-metadata-direct", "http://169.254.169.254/latest/meta-data/",
     "攻击：直连云元数据地址（应禁止，不连接）"),
    ("attack-rebind", "http://rebind.example:18080/loophole",
     "攻击：DNS 重绑定，集合含本机+元数据（整组禁止，零连接）"),
    ("attack-mapped-ipv6", "http://[::ffff:169.254.169.254]/",
     "攻击：IPv4-mapped IPv6 写元数据（解包后禁止）"),
    ("attack-userinfo", "http://alice@169.254.169.254/",
     "攻击：用户名片段混淆（输入面拒绝）"),
    ("attack-integer-ip", "http://2130706433/",
     "攻击：十进制整数 IP=127.0.0.1（规范化后禁止）"),
    ("attack-integer-meta", "http://2852039166/latest/meta-data/",
     "攻击：十进制整数 IP=169.254.169.254（规范化后按元数据禁止）"),
    ("redirect-to-metadata", "http://127.0.0.1:18080/redirect-meta",
     "攻击：先允许后重定向到元数据（每跳重校验，第二跳不连接）"),
    ("redirect-loop", "http://127.0.0.1:18080/loop-a",
     "异常：重定向环（状态冲突）"),
    ("redirect-budget", "http://127.0.0.1:18080/many",
     "异常：重定向预算耗尽（资源耗尽）"),
    ("bad-scheme", "gopher://127.0.0.1/", "异常：非法 scheme（输入错误）"),
]

STAGE_ICON = {
    "parse": "解URL", "dns": "解析DNS", "policy": "策略",
    "connect": "连接", "redirect": "重定向", "kernel": "内核",
}


def render_chain(result: dict) -> str:
    lines = []
    for h in result.get("hops", []):
        icon = STAGE_ICON.get(h["stage"], h["stage"])
        mark = "✓" if h["verdict"] == "allow" else "✗"
        line = f"    hop{h['hop']} {icon} [{mark} {h['verdict']}] {h['reason']}"
        if h.get("resolved"):
            line += f"  候选={h['resolved']}"
        if h.get("matched"):
            line += f"  规则={h['matched']['rule_id']}"
        if h.get("peer_checked"):
            line += f"  对端复核={h['peer_checked']}"
        if h.get("location"):
            line += f"\n        └─Location: {h['location']}"
        if h.get("note"):
            line += f"\n        · {h['note']}"
        lines.append(line)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", action="store_true", help="写结构化结果到 artifacts/demo.jsonl")
    ap.add_argument("--audit", default=str(ROOT / "artifacts" / "audit.sqlite3"))
    args = ap.parse_args()

    (ROOT / "artifacts").mkdir(exist_ok=True)
    bundle = load_policy(str(ROOT / "fixtures" / "policy.json"))
    table = parse_zone(str(ROOT / "fixtures" / "dns" / "primary.zone"))
    audit = AuditLog(args.audit)
    kernel = SecurityKernel(
        PolicyEngine(bundle), FixtureResolver(table), PinnedHTTPConnector(), audit=audit
    )

    jsonl_path = ROOT / "artifacts" / "demo.jsonl"
    jsonl_fh = open(jsonl_path, "w", encoding="utf-8") if args.jsonl else None

    print("=" * 78)
    print(" safeproxy 出站目标校验内核 —— 仅访问本机测试服务的演示")
    print(" 受控 DNS 夹具 + 每跳重校验 + 固定地址连接 + 决策链审计")
    print("=" * 78)

    with OriginServer(18080):
        results = []
        for sid, url, desc in SCENARIOS:
            print(f"\n■ 场景 {sid}: {desc}")
            print(f"  URL: {url}")
            result = kernel.fetch(url)
            results.append((sid, result))
            verdict = result["final_verdict"]
            icon = "ALLOW" if verdict == "allow" else "DENY"
            print(f"  结果: [{icon}] run_id={result['run_id']}")
            if result.get("error"):
                e = result["error"]
                print(f"  失败类别: {e['category']} / {e['code']}")
                print(f"  理由: {e['message']}")
            else:
                print(f"  HTTP {result['status_code']}  对端={result['connected_peer']}"
                      f"  body_sha256={result['body_sha256'][:16]}…")
            print("  决策链:")
            print(render_chain(result))
            if jsonl_fh:
                jsonl_fh.write(json.dumps({"scenario": sid, **result}, ensure_ascii=False) + "\n")

    print("\n" + "=" * 78)
    print(" 审计链验证：")
    v = audit.verify_chain()
    print(f"  ok={v['ok']}  记录数={v['records']}  公钥={v['public_key'][:24]}…")
    print("=" * 78)

    if jsonl_fh:
        jsonl_fh.close()
        print(f"\n结构化结果已写入 {jsonl_path}（含 run_id 与每跳中间状态，可重放）")

    # 演示自检：攻击/异常场景必须全部不是 allow
    bad = [sid for sid, r in results if sid.startswith(("attack", "redirect", "bad"))
           and r["final_verdict"] == "allow"]
    if bad:
        print(f"!! 演示自检失败，以下攻击竟被放行: {bad}", file=sys.stderr)
        return 1
    normal_ok = all(
        r["final_verdict"] == "allow"
        for sid, r in results if sid.startswith("normal")
    )
    if not normal_ok:
        print("!! 正常场景未全部放行", file=sys.stderr)
        return 1
    print("\n演示自检通过：正常场景全部 allow，所有攻击/异常场景均未放行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
