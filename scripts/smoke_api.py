"""端到端冒烟：启动 ASGI 服务（进程内 TestClient），打所有主要端点并打印结论。

不绑定端口、不访问外网，适合 CI 与本机一键验证。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import base64
import json
import time

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.media.rtp import build_rtp


def main() -> int:
    app = create_app(db_path="data/demo.db", log_json=False)
    with TestClient(app) as c:
        print("== /health ==")
        print(json.dumps(c.get("/health").json(), ensure_ascii=False, indent=2))

        print("\n== POST /validate/all（7 个场景）==")
        body = c.post("/validate/all").json()
        for r in body["results"]:
            a = r["arms"]["adaptive"]
            f = r["arms"]["fixed"]
            print(f"  [{'PASS' if r['passed'] else 'FAIL'}] {r['scenario']}: "
                  f"自适应 音频{a['audio']}/空缺{a['gaps']}  "
                  f"固定 音频{f['audio']}/空缺{f['gaps']}")
        print("  all_passed =", body["all_passed"])

        print("\n== POST /validate/plan（原始报文，含 1 个解析失败报文）==")
        packets = [{
            "arrival_us": 1_000_000 + i * 20_000,
            "rtp_base64": base64.b64encode(build_rtp(
                sequence=i, timestamp=i * 160, ssrc=0xABCDEF,
                payload=b"\x00\x00" * 160)).decode(),
        } for i in range(20)]
        packets.append({"arrival_us": 1_500_000,
                        "rtp_base64": base64.b64encode(b"\x80\x60").decode()})
        plan = c.post("/validate/plan", json={"packets": packets}).json()
        print("  自适应帧:", plan["adaptive"]["totals"]["frames"],
              "解析失败:", len(plan["parse_errors"]),
              "oracle:", plan["oracle_passed"])

        print("\n== POST /jobs/scenario（异步作业）==")
        jid = c.post("/jobs/scenario",
                     json={"scenario": "burst_reorder"}).json()["job_id"]
        for _ in range(100):
            row = c.get(f"/jobs/{jid}").json()
            if row["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.02)
        print("  作业", jid, "->", row["status"],
              "passed =", row["result"]["passed"])

    app.state.runner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
