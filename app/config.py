"""Configuration layer.

All settings come from environment variables with explicit defaults so the
project is reproducible from a clean checkout (see README §2). Nothing here
touches a production account or external service.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value is not None and value != "" else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    db_path: str = field(default_factory=lambda: _env("AUDIT_DB_PATH", "data/audit.db"))
    audit_log_path: str = field(default_factory=lambda: _env("AUDIT_LOG_PATH", "logs/audit.log"))
    salt_bytes: int = field(default_factory=lambda: _env_int("AUDIT_SALT_BYTES", 16))
    service_name: str = "local-audit-commitments"


def get_settings() -> Settings:
    # A thin function (not a cached singleton) lets tests point each case at an
    # isolated temporary database via AUDIT_DB_PATH without monkey-patching.
    return Settings()
