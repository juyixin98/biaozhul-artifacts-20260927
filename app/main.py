"""ASGI 入口。

本地运行::

    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from app.api.app import create_app
from app.config import Settings

_settings = Settings.from_env()
app = create_app(
    db_path=_settings.db_path,
    log_level=_settings.log_level,
    log_json=_settings.log_json,
)
