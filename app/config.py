"""Configuration layer.

All knobs live here with conservative defaults. Values can be overridden via
environment variables (prefix ``SUBGUARD_``), e.g. ``SUBGUARD_MIN_DURATION_MS``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw if raw is not None and raw.strip() != "" else default


@dataclass(frozen=True)
class Settings:
    # Diagnostics -----------------------------------------------------------
    min_duration_ms: int = 1000          # cues shorter than this are TOO_SHORT
    max_duration_ms: int = 7000          # cues longer than this are TOO_LONG
    min_gap_ms: int = 1                  # required gap between successive cues
    segment_boundaries_ms: tuple[int, ...] = (30_000, 60_000)
    horizon_ms: int = 90_000             # last segment ends here (hard right edge)

    # Repair budget ---------------------------------------------------------
    max_per_cue_shift_ms: int = 5_000    # |new_start - orig_start| cap per cue
    max_total_shift_ms: int = 60_000     # sum of absolute start-time shifts cap

    # Parsing / serving -----------------------------------------------------
    max_cues: int = 10_000
    db_path: str = "data/subguard.db"
    log_level: str = "INFO"

    @staticmethod
    def from_env() -> "Settings":
        boundaries_raw = _env_str("SUBGUARD_SEGMENT_BOUNDARIES_MS", "")
        if boundaries_raw:
            boundaries = tuple(int(x) for x in boundaries_raw.split(",") if x.strip())
        else:
            boundaries = Settings().segment_boundaries_ms
        return Settings(
            min_duration_ms=_env_int("SUBGUARD_MIN_DURATION_MS", 1000),
            max_duration_ms=_env_int("SUBGUARD_MAX_DURATION_MS", 7000),
            min_gap_ms=_env_int("SUBGUARD_MIN_GAP_MS", 1),
            segment_boundaries_ms=tuple(sorted(boundaries)),
            horizon_ms=_env_int("SUBGUARD_HORIZON_MS", 90_000),
            max_per_cue_shift_ms=_env_int("SUBGUARD_MAX_PER_CUE_SHIFT_MS", 5_000),
            max_total_shift_ms=_env_int("SUBGUARD_MAX_TOTAL_SHIFT_MS", 60_000),
            max_cues=_env_int("SUBGUARD_MAX_CUES", 10_000),
            db_path=_env_str("SUBGUARD_DB_PATH", "data/subguard.db"),
            log_level=_env_str("SUBGUARD_LOG_LEVEL", "INFO").upper(),
        )
