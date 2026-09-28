"""本地开发启动入口：python -m merge3.main 或 uvicorn merge3.main:app。"""
from __future__ import annotations

import uvicorn

from .api.app import create_app
from .config import load_config

app = create_app(load_config())

if __name__ == "__main__":
    cfg = load_config()
    uvicorn.run("merge3.main:app", host=cfg.api.host, port=cfg.api.port, reload=False)
