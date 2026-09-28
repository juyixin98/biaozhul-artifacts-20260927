"""Application configuration.

Configuration is intentionally tiny and dependency-free: values come from
environment variables (prefixed ``SEGBACK_``) and are exposed through an
immutable ``Settings`` dataclass. Tests build their own ``Settings`` with a
temporary database path instead of mutating module globals.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Project root = parent of the ``app`` package directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(f"SEGBACK_{name}", default)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(f"SEGBACK_{name}")
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(f"SEGBACK_{name}")
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    # Fallback cost charged per character of an unknown word. The value is
    # deliberately large so dictionary words win whenever genuinely present,
    # while short unknown spans remain cheaper than long ones (length is
    # explicit, characters are never dropped).
    unknown_char_cost: float = 20.0
    # |best - second_best| below this is reported as "close_gap" instead of
    # "clear"; an empty runner-up is "no_alternative".
    close_gap_threshold: float = 1.0
    # Reject raw input above this size (413 TEXT_TOO_LONG). Sentence-level
    # segmentation callers should split long documents first; a 2000-char cap
    # keeps worst-case (highly ambiguous) latency well below a second. The
    # cap can be raised via SEGBACK_MAX_INPUT_CHARS for batch workloads.
    max_input_chars: int = 2000
    # If the database has no versions at startup, seed this bundled fixture
    # version so the service is usable immediately.
    seed_on_start: bool = True

    @staticmethod
    def from_env() -> "Settings":
        db = Path(_env("DB_PATH", str(PROJECT_ROOT / "data" / "lexicon.db")))
        return Settings(
            db_path=db,
            unknown_char_cost=_env_float("UNKNOWN_CHAR_COST", 20.0),
            close_gap_threshold=_env_float("CLOSE_GAP_THRESHOLD", 1.0),
            max_input_chars=_env_int("MAX_INPUT_CHARS", 2000),
            seed_on_start=_env("SEED_ON_START", "1") not in ("0", "false", "False", ""),
        )
