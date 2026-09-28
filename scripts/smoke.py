"""真实 HTTP 冒烟：跨块 PCM -> finalize -> verify -> events，打印关键结果。

用法: python scripts/smoke.py [base_url]
仅用标准库 urllib + numpy，不依赖 requests。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

import numpy as np

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"


def call(method: str, path: str, body=None, headers=None):
    data = None
    hdr = headers or {}
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
        hdr["Content-Type"] = "application/json"
    elif isinstance(body, (bytes, bytearray)):
        data = bytes(body)
        hdr.setdefault("Content-Type", "application/octet-stream")
    req = urllib.request.Request(BASE + path, data=data, headers=hdr,
                                 method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read())


def main() -> None:
    cfg = {"enter_threshold": 0.03, "exit_threshold": 0.08,
           "min_silence_ms": 100, "min_speech_ms": 50,
           "pad_before_ms": 10, "pad_after_ms": 20}
    st, hdr, body = call("POST", "/jobs", {
        "config": cfg,
        "media": {"container": "pcm", "sample_format": "f32",
                  "sample_rate": 1000, "channels": 1}})
    assert st == 201, body
    jid = body["job_id"]
    print("create:", st, "job_id=", jid, "run_id=", hdr.get("x-run-id"))

    x = np.zeros(1200, dtype=np.float32)
    x[0:200] = 0.5
    x[600:800] = 0.5
    for a in range(0, 1200, 37):
        st, _, b = call("POST", f"/jobs/{jid}/chunks",
                        x[a:min(a + 37, 1200)].tobytes())
        assert st == 200, (a, b)
    print("chunks: uploaded 1200 samples in",
          (1200 + 36) // 37, "requests")

    st, _, body = call("POST", f"/jobs/{jid}/finalize")
    assert st == 200, body
    ivs = [[iv["start"], iv["end"]]
           for iv in body["intervals_committed"]]
    print("finalize:", body["status"], "intervals=", ivs,
          "stats=", body["stats"])
    assert ivs == [[0, 220], [590, 820]]

    st, _, body = call("POST", f"/jobs/{jid}/verify")
    assert st == 200 and body["ok"], body
    print("verify: ok=True; checks=",
          {k: v.get("ok") for k, v in body["checks"].items()})

    st, _, body = call("GET", f"/jobs/{jid}/events?limit=2000")
    kinds = sorted({e["kind"] for e in body["events"]})
    print("events: kinds present =", kinds)
    rationale = [e["reason"] for e in body["events"]
                 if e.get("kind") == "kernel"
                 and e.get("type") == "run_closed"]
    print("sample rationale:", rationale[:2])

    # 错误分类演示：对已收尾作业再写块
    st, _, body = call("POST", f"/jobs/{jid}/chunks", b"\0\0\0\0")
    print("write after finalize ->", st, body["error"]["code"])
    assert st == 409 and body["error"]["code"] == "STATE_CONFLICT"
    print("SMOKE OK")


if __name__ == "__main__":
    main()
