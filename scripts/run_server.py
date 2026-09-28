#!/usr/bin/env python3
"""Runnable service entry: uvicorn wrapper.

Usage:
    LC_DB_PATH=./data/lc.db LC_LOG_DIR=./logs scripts/run_server.py [--port 8000]
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import uvicorn  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Header light client service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    os.environ.setdefault("LC_DB_PATH", os.path.join(ROOT, "data", "lc.db"))
    os.environ.setdefault("LC_LOG_DIR", os.path.join(ROOT, "logs"))
    db_path = os.environ["LC_DB_PATH"]
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    uvicorn.run(
        "lc.app:app",
        host=args.host,
        port=args.port,
        reload=False,
        app_dir=os.path.join(ROOT, "src"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
