"""Configuration layer.

All settings are environment-driven with safe local defaults so the project
runs from a fresh checkout without a production account or external services.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_path: Path
    registry_capacity: int
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        capacity_raw = os.environ.get("ARROWZERO_REGISTRY_CAPACITY", "128")
        try:
            capacity = int(capacity_raw)
            if capacity <= 0:
                raise ValueError
        except ValueError:
            raise ValueError(
                f"ARROWZERO_REGISTRY_CAPACITY must be a positive integer, got {capacity_raw!r}"
            )
        port_raw = os.environ.get("ARROWZERO_PORT", "8000")
        try:
            port = int(port_raw)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise ValueError(f"ARROWZERO_PORT must be 1..65535, got {port_raw!r}")
        return cls(
            db_path=_env_path("ARROWZERO_DB_PATH", "data/arrowzero.db"),
            log_path=_env_path("ARROWZERO_LOG_PATH", "logs/arrowzero.jsonl"),
            registry_capacity=capacity,
            host=os.environ.get("ARROWZERO_HOST", "127.0.0.1"),
            port=port,
        )


def get_settings() -> Settings:
    return Settings.from_env()
