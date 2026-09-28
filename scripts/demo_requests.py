"""端到端演示：对四个合成夹具发真实 HTTP 请求，打印保留区间与失败类别。

用法（先启动服务，或让本脚本自己起 uvicorn）::

    python scripts/demo_requests.py            # 默认 http://127.0.0.1:8000
    python scripts/demo_requests.py --serve    # 本脚本后台拉起服务再请求

输出同时写入 samples/demo_result.txt，便于按文档复核。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

CONFIG = {
    "min_silence_ms": 300,
    "min_activity_ms": 100,
    "pad_ms": 50,
    "merge_gap_ms": 120,
    "enter_threshold": 0.02,
    "exit_threshold": 0.05,
}

EXPECTED = {
    "threshold_pulse": [[155, 380]],
    "long_silence": [[0, 150], [650, 800]],
    "all_silence": [],
    "trailing": [[150, 400]],
}


def wait_ready(base: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            time.sleep(0.25)
    raise RuntimeError("server did not become ready")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8077)
    ap.add_argument("--serve", action="store_true")
    args = ap.parse_args()
    base = f"http://{args.host}:{args.port}"

    proc = None
    if args.serve:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", args.host, "--port", str(args.port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    lines: list[str] = []
    try:
        wait_ready(base)
        ok_all = True
        for name, expected in EXPECTED.items():
            path = Path("samples") / f"{name}.wav"
            with path.open("rb") as fh:
                resp = httpx.post(
                    f"{base}/jobs",
                    files={"file": (path.name, fh, "audio/wav")},
                    data={"config": json.dumps(CONFIG)},
                    timeout=30,
                )
            body = resp.json()
            got = body.get("intervals")
            ok = resp.status_code == 200 and got == expected
            ok_all &= ok
            lines.append(
                f"[{ 'PASS' if ok else 'FAIL' }] {name:16s} "
                f"status={resp.status_code} intervals={got} expected={expected} "
                f"run_id={body.get('run_id')}"
            )
        text = "\n".join(lines)
        print(text)
        Path("samples/demo_result.txt").write_text(text + "\n", encoding="utf-8")
        return 0 if ok_all else 1
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
