"""进程配置：环境变量 + 默认值（零额外依赖）。"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(slots=True)
class Settings:
    db_path: str = "ot.db"
    host: str = "127.0.0.1"
    port: int = 8000
    max_components: int = 10_000
    max_doc_chars: int = 10_000_000
    max_doc_bytes: int = 50_000_000
    history_page_limit: int = 1000

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("OT_DB_PATH", "ot.db"),
            host=os.environ.get("OT_HOST", "127.0.0.1"),
            port=_int("OT_PORT", 8000),
            max_components=_int("OT_MAX_COMPONENTS", 10_000),
            max_doc_chars=_int("OT_MAX_DOC_CHARS", 10_000_000),
            max_doc_bytes=_int("OT_MAX_DOC_BYTES", 50_000_000),
            history_page_limit=_int("OT_HISTORY_PAGE_LIMIT", 1000),
        )
