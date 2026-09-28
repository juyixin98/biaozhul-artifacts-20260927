"""Service configuration sourced from environment variables.

All knobs have defaults so ``uvicorn`` starts with zero configuration; every
value is overridable for tests (small limits, temp DB path).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .normalizer import DEFAULT_FORM


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("TEXTINDEX_DB", "data/textindex.db")
    log_path: str = os.environ.get("TEXTINDEX_LOG", "logs/service.jsonl")
    max_document_bytes: int = _env_int("TEXTINDEX_MAX_DOC_BYTES", 1 << 20)
    max_clusters: int = _env_int("TEXTINDEX_MAX_CLUSTERS", 1_000_000)
    default_normalization: str = os.environ.get(
        "TEXTINDEX_NORMALIZATION", DEFAULT_FORM
    )
