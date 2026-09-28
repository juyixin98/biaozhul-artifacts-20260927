"""ASGI 入口：uvicorn diffanalyzer.main:app"""

from __future__ import annotations

from .api import create_app
from .service import build_default_service

service, store, auditor, cfg = build_default_service()
app = create_app(service, store, auditor, max_trace_steps=cfg.max_trace_steps)
