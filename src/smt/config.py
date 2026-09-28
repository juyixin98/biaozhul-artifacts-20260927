"""Application configuration loaded from environment variables.

Kept dependency-free (no pydantic-settings) so the service runs with a plain
stdlib + fastapi install.  Every value has a safe local-development default;
nothing here needs a production account.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Database
    sqlite_path: str = field(default_factory=lambda: _env("SMT_SQLITE_PATH", "data/runtime/state.db"))

    # HMAC key used to sign journal rows / journal export files.
    # Base64 or raw utf-8; empty means a fixed development key is used.
    journal_hmac_key: str = field(
        default_factory=lambda: _env("SMT_JOURNAL_HMAC_KEY", "dev-only-journal-key-change-me")
    )

    # API
    host: str = field(default_factory=lambda: _env("SMT_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("SMT_PORT", 8080))

    # Logging: "json" (one structured record per line) or "text".
    log_format: str = field(default_factory=lambda: _env("SMT_LOG_FORMAT", "json"))
    log_level: str = field(default_factory=lambda: _env("SMT_LOG_LEVEL", "INFO"))

    # Tree parameters (the tree spec); fixed for the life of a database.
    key_bits: int = 256

    def validate(self) -> None:
        if self.key_bits != 256:
            # The on-disk version tag and proof "depth" semantics assume 256.
            raise ValueError("only key_bits=256 is supported by this build")


def get_settings() -> Settings:
    settings = Settings()
    settings.validate()
    return settings
