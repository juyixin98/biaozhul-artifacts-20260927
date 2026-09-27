"""uvicorn 入口：`uvicorn app.main:app`。"""
from __future__ import annotations

from .api import create_app

app = create_app()
