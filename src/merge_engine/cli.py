"""本地服务启动入口。

用法：
    python -m merge_engine.cli --db var/merge.db --journal var/journal --reload
仅依赖本地文件，无外部账号。
"""
from __future__ import annotations

import argparse

import uvicorn

from .api import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Composite-key MERGE engine")
    parser.add_argument("--db", default="var/merge.db")
    parser.add_argument("--journal", default="var/journal")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--allow-fault-injection", action="store_true",
                        help="enable fault_point for local demos (never in prod)")
    args = parser.parse_args()

    app = create_app(args.db, args.journal,
                     allow_fault_injection=args.allow_fault_injection)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
