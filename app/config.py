"""Process configuration loaded from environment variables with fixed defaults.

Everything is local-only: SQLite file and a local artifact directory.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("PNV_DB_PATH", "data/validation.db")))
    artifact_dir: Path = field(default_factory=lambda: Path(os.environ.get("PNV_ARTIFACT_DIR", "data/artifacts")))
    # Default Parquet data page size used when clients do not pin one.
    default_page_size_bytes: int = int(os.environ.get("PNV_DEFAULT_PAGE_SIZE", "1024"))
    log_level: str = field(default_factory=lambda: os.environ.get("PNV_LOG_LEVEL", "INFO"))
    log_json: bool = field(default_factory=lambda: _bool("PNV_LOG_JSON", True))
    # Maximum number of records a single validation request may carry.
    max_records: int = int(os.environ.get("PNV_MAX_RECORDS", "20000"))


def get_settings() -> Settings:
    return Settings()
