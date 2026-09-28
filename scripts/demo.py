#!/usr/bin/env python3
"""本地端到端演示：起真实 HTTP 服务 → 提交合成夹具 → 出碰撞见证 → 修复键 → 核验碰撞消失。

用法：
    python scripts/demo.py
    python scripts/demo.py --keep-db data/demo.sqlite3   # 保留库，便于手工复现

演示全程不访问网络业务方，所有身份令牌均为合成数据。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = json.loads((ROOT / "fixtures" / "scenarios.json").read_text(encoding="utf-8"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(client: httpx.Client, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = client.get("/health")
            if r.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f"服务未在 {timeout}s 内就绪: {last}")


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def submit_scenario(base: str, scenario: dict, policy_key: str | None = None) -> str:
    sid = scenario["id"]
    policy = FIXTURES["policies"][policy_key or scenario["policy"]]
    run_id = f"demo-{sid.lower()}"
    client = httpx.Client(base_url=base)
    # 每次演示用独立 run；若库保留导致已存在则加时间戳后缀
    if client.post("/v1/runs", json={"run_id": run_id, "label": scenario["kind"]}).status_code == 409:
        run_id = f"{run_id}-{int(time.time())}"
        r = client.post("/v1/runs", json={"run_id": run_id, "label": scenario["kind"]})
        r.raise_for_status()
    r = client.put(f"/v1/runs/{run_id}/policy", json=policy)
    r.raise_for_status()
    r = client.post(f"/v1/runs/{run_id}/evidence", json={"evidence": scenario["evidence"]})
    r.raise_for_status()
    return run_id


def show_witness(run_id: str, analysis: dict) -> None:
    print(f"  证据数={analysis['analyzed_evidence']} 同键组={analysis['collision_groups']} "
          f"发现={len(analysis['findings'])}")
    for f in analysis["findings"]:
        p = f["pair"]
        a, b = p["request_a"], p["request_b"]
        print(f"  [{f['severity'].upper()}] 见证 {p['witness_id']}")
        print(f"    维度       : {f['dimension']}  原因码: {f['reason']}")
        print(f"    请求 A     : {p['evidence_a']} {a['method']} {a['path']} "
              f"头={json.dumps(a['headers'], ensure_ascii=False)}")
        print(f"    请求 B     : {p['evidence_b']} {b['method']} {b['path']} "
              f"头={json.dumps(b['headers'], ensure_ascii=False)}")
        print(f"    响应体摘要 : {p['response_body_a_sha256'][:16]}… != "
              f"{p['response_body_b_sha256'][:16]}…")
        ka, kb = p["key_components_a"], p["key_components_b"]
        print(f"    键的差异分量: A.vary={ka['vary']} A.identity={list(ka['identity'])} "
              f"| B.vary={kb['vary']} B.identity={list(kb['identity'])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-db", default=None, help="保留 SQLite 到指定路径")
    args = ap.parse_args()

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    db_path = args.keep_db or str(Path(tempfile.mkdtemp(prefix="audit-demo-")) / "demo.sqlite3")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    master_key = "demo-master-key-synthetic-only-000000000000"

    env = dict(os.environ, AUDIT_DB=db_path, AUDIT_MASTER_KEY=master_key,
               AUDIT_PORT=str(port))
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "run_server.py")],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    demo_log = ROOT / "data" / "demo_last_run.jsonl"
    demo_log.parent.mkdir(exist_ok=True)
    log_records: list[dict] = []

    try:
        with httpx.Client(base_url=base, timeout=10) as client:
            wait_ready(client)
            hr("0) 服务健康检查")
            print(" ", client.get("/health").json())

            # 共享缓存缺陷策略：S1/S2/S3/S5/S6 五个反例
            shared_ids = ["S1-language", "S2-encoding", "S3-authz-identity",
                          "S5-vary-star", "S6-missing-vary", "S7-static-ok",
                          "S8-shared-cookie"]
            hr("1) 在【缺陷策略】下逐个提交反例，期望得到具体碰撞见证")
            collision_run_ids = []
            for sid in shared_ids:
                sc = next(s for s in FIXTURES["scenarios"] if s["id"] == sid)
                run_id = submit_scenario(base, sc)
                analysis = client.post(f"/v1/runs/{run_id}/analyze").json()
                expected = sc["expected_broken"]["findings_count"]
                got = len(analysis["findings"])
                status = "OK " if got == expected else "FAIL"
                print(f"  [{status}] {sid}: 期望 {expected} 个发现，实际 {got} 个 —— {sc['kind']}")
                show_witness(run_id, analysis)
                log_records.append({"phase": "broken", "scenario": sid, "run_id": run_id,
                                    "expected_findings": expected, "actual_findings": got,
                                    "findings": analysis["findings"],
                                    "rationale": analysis["decision_rationale"]})
                if expected:
                    collision_run_ids.append(run_id)

            hr("2) 修复键：对身份反例 S3 执行 remediate，核验碰撞消失")
            s3 = next(s for s in FIXTURES["scenarios"] if s["id"] == "S3-authz-identity")
            run_id = f"demo-{s3['id'].lower()}"
            rem = client.post(f"/v1/runs/{run_id}/remediate").json()
            print(f"  修复前发现数={len(rem['before']['findings'])}  "
                  f"修复后发现数={len(rem['after']['findings'])}  "
                  f"collision_gone={rem['collision_gone']}")
            print(f"  被消除的见证: {rem['cleared_witness_ids']}")
            print(f"  修复后键分量（身份已入键）:")
            for eid, comp in rem["after"]["derived_keys"].items():
                print(f"    {eid}: identity={json.dumps(comp['identity'], ensure_ascii=False)}")
            log_records.append({"phase": "remediation", "run_id": run_id, "result": {
                "collision_gone": rem["collision_gone"],
                "cleared": rem["cleared_witness_ids"],
                "residual": rem["residual_witness_ids"]}})

            hr("3) 私有缓存夹具 S4：不同 Cookie 会话必须天然分到不同键")
            s4 = next(s for s in FIXTURES["scenarios"] if s["id"] == "S4-private-cookie")
            run4 = submit_scenario(base, s4)
            a4 = client.post(f"/v1/runs/{run4}/analyze").json()
            print(f"  findings={len(a4['findings'])}（期望 0）")
            for eid, comp in a4["derived_keys"].items():
                print(f"    {eid}: identity={json.dumps(comp['identity'], ensure_ascii=False)}")
            assert len(a4["findings"]) == 0
            log_records.append({"phase": "private", "scenario": "S4-private-cookie",
                                "run_id": run4, "actual_findings": 0})

            hr("4) 审计事件链核验（防篡改、可重放）")
            v = client.get(f"/v1/runs/{run_id}/verify").json()
            print(" ", v)
            assert v["ok"]
            events = client.get(f"/v1/runs/{run_id}/events").json()
            print(f"  事件 {len(events)} 条，类型序列: {[e['event_type'] for e in events]}")
            log_records.append({"phase": "chain", "run_id": run_id, "verify": v})

            hr("5) 复现入口")
            print(f"  数据库: {db_path}")
            print(f"  可重放 run_id 示例: {collision_run_ids}")
            print("  重放命令（服务使用同一 AUDIT_MASTER_KEY）:")
            print(f"    AUDIT_DB={db_path} AUDIT_MASTER_KEY='{master_key}' "
                  f"{sys.executable} run_server.py")
            print(f"    curl -s http://127.0.0.1:8000/v1/runs/{collision_run_ids[0]}/analysis | python -m json.tool")

        demo_log.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in log_records),
            encoding="utf-8",
        )
        hr(f"演示日志（含运行编号/中间状态/判断理由）: {demo_log}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
