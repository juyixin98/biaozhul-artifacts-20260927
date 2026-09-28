"""本地启动入口：python -m app.main 或 uvicorn app.api.main:app。"""
from __future__ import annotations

import uvicorn

from .api.main import app

__all__ = ["app"]


if __name__ == "__main__":
    uvicorn.run("app.api.main:app", host="127.0.0.1", port=8080, reload=False)
