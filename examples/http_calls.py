"""示例：通过 HTTP API 演示完整提交流程（需要服务端已启动）。

先在另一个终端启动：
    . .venv/bin/activate
    python -m uvicorn otbackend.api:main --factory --reload \
        （或：OT_DB_PATH=/tmp/demo.db python -m otbackend.api）

然后运行：
    python examples/http_calls.py

也可以在脚本内自起服务（设环境变量 OT_START_SERVER=1）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

BASE = os.environ.get("OT_BASE_URL", "http://127.0.0.1:8123")
PORT = int(os.environ.get("OT_EXAMPLE_PORT", "8123"))


def pretty(title, resp):
    print(f"\n== {title} -> {resp.status_code} ==")
    try:
        print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
    except Exception:
        print(resp.text)


def main() -> None:
    proc = None
    if os.environ.get("OT_START_SERVER") == "1":
        env = dict(os.environ, OT_DB_PATH="/tmp/ot_demo.db", OT_PORT=str(PORT))
        proc = subprocess.Popen([sys.executable, "-m", "otbackend.api"], env=env)
        time.sleep(1.5)

    try:
        with httpx.Client(base_url=BASE, timeout=10) as c:
            pretty("建文档", c.post("/documents",
                                   json={"doc_id": "demo", "initial_text": "hello"}))
            pretty("alice 插入（r0）", c.post("/documents/demo/submit", json={
                "base_rev": 0, "client_id": "alice", "client_op_id": 1,
                "ops": [{"type": "ins", "pos": 5, "text": "!"}]}))
            pretty("bob 并发插入（同样基于 r0）", c.post("/documents/demo/submit", json={
                "base_rev": 0, "client_id": "bob", "client_op_id": 1,
                "ops": [{"type": "ins", "pos": 0, "text": "("}]}))
            pretty("bob 重复提交（幂等）", c.post("/documents/demo/submit", json={
                "base_rev": 0, "client_id": "bob", "client_op_id": 1,
                "ops": [{"type": "ins", "pos": 0, "text": "("}]}))
            pretty("越界删除（应 409）", c.post("/documents/demo/submit", json={
                "base_rev": 2, "client_id": "x", "client_op_id": 1,
                "ops": [{"type": "del", "pos": 99, "length": 1}]}))
            pretty("历史", c.get("/documents/demo/history"))
            pretty("当前视图", c.get("/documents/demo"))
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait()


if __name__ == "__main__":
    main()
