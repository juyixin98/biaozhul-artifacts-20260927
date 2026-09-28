#!/usr/bin/env python3
"""End-to-end service demonstration: submits normal and abnormal jobs to a
running API and stores the responses as JSON for review.

Start the server first:  scripts/run_server.sh
Then run:                .venv/bin/python scripts/demo_client.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

import os

BASE = os.environ.get("APP_BASE", "http://127.0.0.1:8000")
OUT = Path(__file__).resolve().parent.parent / "var" / "demo"


CASES = {
    "01_direct_concat": {
        "segments": [
            {"path": "seg_ok_a.json"},
            {"path": "seg_ok_b.json"},
        ]},
    "02_non_keyframe_trim": {
        "segments": [{
            "path": "seg_midgop.json",
            "trim_in": {"num": 2, "den": 15},
            "trim_out": {"num": 4, "den": 15},
        }]},
    "03_open_gop_cut": {
        "segments": [{
            "path": "seg_open_gop.json",
            "trim_in": {"num": 1, "den": 5},
        }]},
    "04_audio_encoder_delay": {
        "segments": [{"path": "seg_audio_delay.json"}]},
    "05_timebase_mismatch": {
        "segments": [
            {"path": "seg_ok_a.json"},
            {"path": "seg_tb_mismatch.json"},
        ]},
    "06_codec_mismatch": {
        "segments": [
            {"path": "seg_ok_a.json"},
            {"path": "seg_codec_mismatch.json"},
        ]},
    "07_dangling_reference": {
        "segments": [{"path": "seg_dangling_ref.json"}]},
    "08_missing_file": {
        "segments": [{"path": "no_such_file.json"}]},
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    summary = []
    with httpx.Client(base_url=BASE, timeout=10) as c:
        print(c.get("/version").json())
        for name, body in CASES.items():
            resp = c.post("/jobs", json=body)
            record = {"request": body, "http_status": resp.status_code,
                      "body": resp.json()}
            if resp.status_code == 201:
                job_id = record["body"]["job_id"]
                if "plan" in record["body"]:
                    record["validate"] = c.post(
                        f"/jobs/{job_id}/validate").json()
                record["events"] = c.get(
                    f"/jobs/{job_id}/events").json()["events"]
            (OUT / f"{name}.json").write_text(
                json.dumps(record, indent=2, ensure_ascii=False) + "\n")
            job = record["body"]
            summary.append({
                "case": name,
                "http": resp.status_code,
                "status": job.get("status"),
                "decision": job.get("decision"),
                "error_category": job.get("error_category")
                                  or job.get("detail", {}).get("category"),
            })
        (OUT / "00_summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    for row in summary:
        print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
