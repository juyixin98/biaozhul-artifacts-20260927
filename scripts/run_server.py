#!/usr/bin/env python3
"""Run the clockalign API service.

Usage: python scripts/run_server.py [--host H] [--port P]
Config: CLOCKALIGN_CONFIG=/path/to.yaml (default config/default.yaml)
Data:    CLOCKALIGN_HOME=/path/to/dir  (default ./data)
"""
import argparse

import uvicorn

from clockalign.config import load_config

if __name__ == "__main__":
    cfg = load_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=cfg.app.host)
    parser.add_argument("--port", type=int, default=cfg.app.port)
    args = parser.parse_args()
    uvicorn.run("clockalign.api:app", host=args.host, port=args.port,
                reload=False, log_config=None)
