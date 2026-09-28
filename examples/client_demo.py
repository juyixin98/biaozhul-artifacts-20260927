#!/usr/bin/env python3
"""服务调用示例：提交三类作业（可行直拼 / 必须转码 / 异常输入）并打印校验视图。

运行：先启动服务（见 README），再执行 .venv/bin/python examples/client_demo.py
"""
from __future__ import annotations

import json
import sys

import httpx

BASE = "http://127.0.0.1:8000"


def submit(client: httpx.Client, name: str, body: dict) -> dict:
    r = client.post("/jobs", json=body)
    r.raise_for_status()
    job_id = r.json()["job_id"]
    detail = client.get(f"/jobs/{job_id}").json()
    verify = client.get(f"/jobs/{job_id}/verify").json()
    print(f"\n=== {name} (job_id={job_id}) ===")
    print("status:", detail["status"])
    if detail["status"] == "failed":
        print("error_code:", detail["error_code"])
        print("error:", (detail["error"] or "").splitlines()[0])
        return verify
    plan = detail["plan"]
    print("feasible:", plan["feasible"], "| mode:", plan["mode"],
          "| output_tb:", plan["output_time_base"],
          "| duration_sec:", plan["output_duration_sec"])
    for f in plan["findings"]:
        print(f"  [{f['severity']}] {f['code']}: {f['message']}")
        if f.get("evidence"):
            print("      evidence:", json.dumps(f["evidence"], ensure_ascii=False))
    print("verify checks:")
    for c in verify.get("checks", []):
        print("  ", json.dumps(c, ensure_ascii=False))
    return verify


def main() -> int:
    try:
        with httpx.Client(base_url=BASE, timeout=10) as client:
            heal = client.get("/health").json()
            print("service:", heal)

            # 1) 可行直拼：25fps + 30fps，统一到 1/150
            submit(client, "混合时基直拼", {
                "sources": ["tb_mixed_a", "tb_mixed_b"],
                "output_container": "mp4",
                "job_id": "demo-mixed-tb",
            })

            # 2) 开放 GOP + 非关键帧起切（首片段，允许预滚）
            submit(client, "开放GOP预滚", {
                "sources": ["open_gop"],
                "output_container": "mp4",
                "job_id": "demo-open-gop",
            })

            # 3) 拼接边界非关键帧裁剪 → 必须转码（不得伪装直拼）
            submit(client, "边界非关键帧(需转码)", {
                "sources": ["nonkey_a", "nonkey_b"],
                "output_container": "mp4",
                "job_id": "demo-nonkey",
            })

            # 4) A/V 尾部填充
            submit(client, "音频尾部填充", {
                "sources": ["av_tailpad"],
                "output_container": "mp4",
                "job_id": "demo-tailpad",
            })

            # 5) 异常输入：不存在的夹具 → status=failed
            submit(client, "异常输入", {
                "sources": ["definitely_missing_clip"],
                "output_container": "mp4",
                "job_id": "demo-missing",
            })
    except httpx.ConnectError:
        print("无法连接服务，请先运行：.venv/bin/python -m mediaconcat.main", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
