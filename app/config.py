"""Configuration loaded from environment variables with safe local defaults.

No cloud credentials anywhere — this service is fully local by construction.
Environment overrides use the ``AC_`` prefix.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULTS = {
    "AC_DB_PATH": "./data/ac.db",
    "AC_SECRET": "local-dev-secret-change-me",
    "AC_MAX_PATTERNS": "5000",
    "AC_MAX_PATTERN_BYTES": "65536",
    "AC_MAX_CHUNK_BYTES": "4194304",       # 4 MiB per streamed chunk
    "AC_DEFAULT_PAGE_LIMIT": "100",
    "AC_MAX_PAGE_LIMIT": "1000",
}


def _env_int(key: str) -> int:
    return int(os.environ.get(key, DEFAULTS[key]))


@dataclass(frozen=True)
class Settings:
    db_path: str
    secret: str
    max_patterns: int
    max_pattern_bytes: int
    max_chunk_bytes: int
    default_page_limit: int
    max_page_limit: int

    @classmethod
    def from_env(cls, env: os._Environ | None = None) -> "Settings":
        env = env if env is not None else os.environ
        return cls(
            db_path=env.get("AC_DB_PATH", DEFAULTS["AC_DB_PATH"]),
            secret=env.get("AC_SECRET", DEFAULTS["AC_SECRET"]),
            max_patterns=int(env.get("AC_MAX_PATTERNS",
                                     DEFAULTS["AC_MAX_PATTERNS"])),
            max_pattern_bytes=int(env.get("AC_MAX_PATTERN_BYTES",
                                          DEFAULTS["AC_MAX_PATTERN_BYTES"])),
            max_chunk_bytes=int(env.get("AC_MAX_CHUNK_BYTES",
                                        DEFAULTS["AC_MAX_CHUNK_BYTES"])),
            default_page_limit=int(
                env.get("AC_DEFAULT_PAGE_LIMIT",
                        DEFAULTS["AC_DEFAULT_PAGE_LIMIT"])),
            max_page_limit=int(env.get("AC_MAX_PAGE_LIMIT",
                                       DEFAULTS["AC_MAX_PAGE_LIMIT"])),
        )

    def absolute_db_path(self) -> Path:
        return Path(self.db_path).expanduser().resolve()
