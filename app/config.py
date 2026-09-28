"""Centralised configuration.

Everything is local: a SQLite file under the working directory and synthetic
fixtures only. Values can be overridden through environment variables so the
acceptance run is reproducible from a clean directory.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = "parquet-nested-verifier"
    db_path: Path = field(
        default_factory=lambda: Path(os.environ.get("PV_DB_PATH", "./data/verifier.db"))
    )
    # Soft target for data pages, measured in *leaf slots* (D/R level pairs).
    # Pages are only cut on record boundaries (R == 0); a single record that
    # exceeds the target is carried in an oversized page by design.
    page_slot_target: int = int(os.environ.get("PV_PAGE_SLOT_TARGET", "1000"))
    # When comparing against the PyArrow-written file, force this page version.
    parquet_page_version: str = os.environ.get("PV_PAGE_VERSION", "2.0")
    parquet_compression: str = os.environ.get("PV_COMPRESSION", "NONE")
    request_id_header: str = "X-Request-ID"
    max_schema_nodes: int = 512
    max_records: int = 100_000
    strict_logical_types: bool = _env_bool("PV_STRICT_TYPES", True)

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
