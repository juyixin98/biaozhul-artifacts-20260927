#!/usr/bin/env python3
"""通过 FastAPI TestClient（完整 ASGI/HTTP 层）跑一遍正常+异常案例，
把可复核结果写入 logs/verified_run/。不依赖外部端口。

用法：MEDIACONCAT_RUN_ID=verified-demo .venv/bin/python scripts/verify_run.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.setdefault("MEDIACONCAT_RUN_ID", "verified-demo")
os.environ.setdefault("MEDIACONCAT_DB", "data/verified.db")
os.environ.setdefault("MEDIACONCAT_LOG_DIR", "logs")
os.environ.setdefault("MEDIACONCAT_FIXTURES", "fixtures")

from fastapi.testclient import TestClient  # noqa: E402

from mediaconcat.api import app  # noqa: E402

OUT = pathlib.Path("logs/verified_run")
OUT.mkdir(parents=True, exist_ok=True)

JOBS = [
    ("ok-mixed-tb", ["tb_mixed_a", "tb_mixed_b"], "mp4"),
    ("ok-open-gop", ["open_gop"], "mp4"),
    ("ok-tailpad", ["av_tailpad"], "mp4"),
    ("ok-aac-delay", ["aac_delay"], "mp4"),
    ("bad-nonkey", ["nonkey_a", "nonkey_b"], "mp4"),
    ("bad-open-gop-lost", ["open_gop_lost_a", "open_gop_lost_b"], "mp4"),
    ("bad-ts-clock", ["tb12800"], "mpegts"),
    ("bad-aac-ts", ["aac_delay_ts"], "mpegts"),
    ("err-missing", ["no_such_fixture_zzz"], "mp4"),
]


def main() -> None:
    summary_lines = ["# HTTP 层验证结果（TestClient）\n"]
    with TestClient(app) as client:
        health = client.get("/health").json()
        summary_lines.append(f"health: {health}\n")
        for job_id, sources, container in JOBS:
            r = client.post("/jobs", json={
                "job_id": job_id, "sources": sources,
                "output_container": container,
            })
            assert r.status_code == 201, (job_id, r.status_code, r.text)
            verify = client.get(f"/jobs/{job_id}/verify").json()
            line = {
                "job_id": job_id, "status": verify["status"],
                "feasible": verify.get("feasible"), "mode": verify.get("mode"),
                "errors": [e["code"] for e in verify.get("errors", [])],
                "warnings": [w["code"] for w in verify.get("warnings", [])],
            }
            summary_lines.append(json.dumps(line, ensure_ascii=False))
            (OUT / f"{job_id}.verify.json").write_text(
                json.dumps(verify, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            detail = client.get(f"/jobs/{job_id}").json()
            (OUT / f"{job_id}.job.json").write_text(
                json.dumps(detail, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    (OUT / "summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    print("\n".join(summary_lines))


if __name__ == "__main__":
    main()
