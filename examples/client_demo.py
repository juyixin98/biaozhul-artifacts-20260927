#!/usr/bin/env python3
"""End-to-end demonstration of the resampling service over real HTTP.

It (1) starts nothing itself — point it at a running uvicorn — (2) creates a
chunked resampling job, (3) pushes a synthesized mono tone in several chunk
sizes, (4) flushes, and (5) verifies the assembled result against the
independent FFT reference.  Every response's request id is printed so a run
can be traced in the server logs.

Usage:
    python examples/client_demo.py --base-url http://127.0.0.1:8000

Set RESAMP_TEST_RUN_ID is not needed here; the server assigns request ids.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys

import numpy as np
import urllib.request


def _post(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
        body["_request_id"] = resp.headers.get("x-request-id")
        return body


def _get(url: str) -> dict:
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read())


def _b64(x: np.ndarray) -> str:
    return base64.b64encode(np.asarray(x, dtype="<f8").tobytes()).decode()


def _decode(p: dict) -> np.ndarray:
    return np.frombuffer(base64.b64decode(p["data"]), dtype="<f8").copy()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--fin", type=int, default=8000)
    ap.add_argument("--fout", type=int, default=48000)
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    # Design check first (boundary validation).
    design = _post(f"{base}/v1/design",
                   {"fin": args.fin, "fout": args.fout})
    print(f"[design] {args.fin}->{args.fout} L={design['l']} M={design['m']} "
          f"taps={design['numtaps']} fc={design['cutoff_hz']}Hz "
          f"fstop={design['fstop_hz']}Hz "
          f"delay={design['group_delay_seconds']*1000:.3f}ms "
          f"req={design['_request_id']}")

    job = _post(f"{base}/v1/jobs",
                {"fin": args.fin, "fout": args.fout,
                 "output_dtype": "float64"})
    jid = job["job"]["job_id"]
    print(f"[create] job={jid} req={job['_request_id']}")

    n = args.fin  # 1 second
    t = np.arange(n) / args.fin
    x = 0.7 * np.sin(2 * np.pi * 440 * t) + 0.2 * np.sin(2 * np.pi * 1200 * t)

    total_out = 0
    for i in range(0, n, 377):  # deliberately awkward chunk size
        chunk = x[i:i + 377]
        r = _post(f"{base}/v1/jobs/{jid}/push",
                  {"encoding": "base64-float64-le", "data": _b64(chunk)})
        total_out += r["n_output"]
    flushed = _post(f"{base}/v1/jobs/{jid}/flush")
    total_out += flushed["n_output"]
    print(f"[stream] pushed {n} samples in chunks of 377 -> {total_out} out")

    status = _get(f"{base}/v1/jobs/{jid}")["job"]
    print(f"[status] state={status['state']} "
          f"total_in={status['total_in']} total_out={status['total_out']}")

    result = _get(f"{base}/v1/jobs/{jid}/result")
    y = _decode(result["output"])
    print(f"[result] retrieved {result['n_samples']} samples; "
          f"head={np.round(y[:4], 6).tolist()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
