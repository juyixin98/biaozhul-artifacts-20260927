"""Application configuration.

All settings have local/synthetic defaults so the service runs without any
production account. Environment variables are prefixed with ``SEG_``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "app" / "data" / "dictionary.db"
DEFAULT_SEED_PATH = PROJECT_ROOT / "app" / "data" / "seed_dictionary.json"


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("SEG_DB_PATH", str(DEFAULT_DB_PATH))))
    seed_path: Path = field(default_factory=lambda: Path(os.environ.get("SEG_SEED_PATH", str(DEFAULT_SEED_PATH))))

    # Segmentation cost model.
    unknown_char_cost: float = float(os.environ.get("SEG_UNKNOWN_CHAR_COST", "8.0"))
    # Cost of every word is clipped so negative/zero costs never destabilise the DAG.
    min_word_cost: float = float(os.environ.get("SEG_MIN_WORD_COST", "0.01"))

    # Longest dictionary word emitted as a DAG edge (bounds trie scans).
    max_word_length: int = int(os.environ.get("SEG_MAX_WORD_LENGTH", "32"))

    # Publish an initial dictionary version from the synthetic seed file when
    # the database has no published version yet.
    auto_seed: bool = field(default_factory=lambda: _as_bool(os.environ.get("SEG_AUTO_SEED", "true")))

    # Logging.
    log_level: str = field(default_factory=lambda: os.environ.get("SEG_LOG_LEVEL", "INFO"))

    # When false, request/response text never reaches the logs (only hashes/lengths).
    log_reveal_text: bool = field(default_factory=lambda: _as_bool(os.environ.get("SEG_LOG_REVEAL_TEXT", "false")))


def get_settings() -> Settings:
    """Return a fresh Settings instance (reads env at call time, easy to test)."""
    return Settings()
