"""Configuration layer.

All settings are read from environment variables with local-safe defaults;
no production accounts or external services are involved.
"""
from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass

import fastapi
import pyarrow
import pytest


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        # A malformed env var is a deployment error; fail loudly rather than
        # silently pretending the service is healthy with a wrong limit.
        raise ValueError(f"environment variable {name}={raw!r} is not an integer") from None


@dataclass(frozen=True)
class Settings:
    sqlite_path: str
    log_dir: str
    # Encoding-policy defaults (may be overridden per request).
    default_target_width: int
    default_width_policy: str  # "reject" | "expand"
    default_sort_policy: str   # fixed policy: "type_then_value"
    log_level: str

    def version_info(self) -> dict:
        import sqlite3

        return {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "pyarrow": pyarrow.__version__,
            "fastapi": fastapi.__version__,
            "pytest": pytest.__version__,
            "sqlite": sqlite3.sqlite_version,
            "service": "dictsvc 1.0.0",
        }


def get_settings() -> Settings:
    return Settings(
        sqlite_path=os.environ.get("DICTSVC_SQLITE_PATH", "data/dictsvc.db"),
        log_dir=os.environ.get("DICTSVC_LOG_DIR", "data/logs"),
        default_target_width=int(_int_env("DICTSVC_DEFAULT_WIDTH", 8)),
        default_width_policy=os.environ.get("DICTSVC_DEFAULT_WIDTH_POLICY", "reject"),
        default_sort_policy="type_then_value",
        log_level=os.environ.get("DICTSVC_LOG_LEVEL", "INFO"),
    )
