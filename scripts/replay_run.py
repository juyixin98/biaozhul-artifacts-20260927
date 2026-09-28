#!/usr/bin/env python3
"""按 run_id 从审计存储重放一次决策。

用途：复现失败时，仅凭 state 目录就能还原
运行编号、每跳关键中间状态、最终裁决与失败类别/原因，
并验证证据链 Ed25519 签名是否完整。

用法::

    python -m scripts.replay_run <state_audit_dir> <run_id> [--json]
    python -m scripts.replay_run state/demo/audit --last
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.audit import AuditStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audit_dir", help="AuditStore 目录（含 audit.sqlite3）")
    ap.add_argument("run_id", nargs="?", help="运行编号；与 --last 二选一")
    ap.add_argument("--last", action="store_true", help="重放最近一次 run")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON 而非文本摘要")
    args = ap.parse_args()

    if not args.run_id and not args.last:
        ap.error("必须提供 run_id 或 --last")

    store = AuditStore(args.audit_dir)
    if args.last:
        latest = store.list_runs(limit=1)
        if not latest:
            print("审计库为空", file=sys.stderr)
            return 2
        run_id = latest[0]["run_id"]
    else:
        run_id = args.run_id

    data = store.get_run(run_id)
    if data is None:
        print(f"未找到 run: {run_id}", file=sys.stderr)
        return 2
    verify = store.verify_run(run_id)

    if args.json:
        print(json.dumps({"verify": verify, "run": data}, ensure_ascii=False, indent=2))
        return 0 if verify["valid"] else 3

    print(f"run_id    : {data['run_id']}")
    print(f"request   : {data['requested_url']}")
    print(f"verdict   : {data['verdict']}  status={data['status']}  "
          f"duration_ms={data['duration_ms']}")
    print(f"signature : valid={verify['valid']}  sha256={verify['digest_sha256']}")
    if data.get("failure"):
        f = data["failure"]
        print(f"failure   : [{f['kind']}] {f['reason']} -- {f['message']}")
    print("-" * 72)
    for i, hop in enumerate(data["hops"], 1):
        parsed = hop["parsed"]
        print(f"hop {i}: {hop['url']}  outcome={hop['outcome']}")
        print(f"    host={parsed['host']} ({parsed['host_kind']}) port={parsed['port']}")
        if hop.get("resolved"):
            for a in hop["resolved"]["addresses"]:
                print(f"    dns: {a['canonical_ip']} {a['family']} "
                      f"tags={a['tags']} unwrapped={a['unwrapped_from']} "
                      f"source={hop['resolved']['source']}")
        if hop.get("policy"):
            for d in hop["policy"]["addresses"]:
                print(f"    rule: {d['ip']:18} -> {d['action']:5} by {d['rule_id']}")
        for a in hop.get("attempts", []):
            print(f"    connect: {a['ip']}:{a['port']} via {a.get('transport')} "
                  f"peer={a.get('peer_ip')} result={a['result']}")
    print("-" * 72)
    print("决策理由（finish）:")
    finish = data["decision_chain"][-1]
    print(f"    {finish['verdict']} {finish['reason']}")
    return 0 if verify["valid"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
