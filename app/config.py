"""Runtime configuration sourced from environment variables.

Only operational knobs live here (storage path, request size, channel counts).
Measurement parameters are fixed by the standards and live in
``r128_constants.py`` on purpose.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .r128_constants import DEFAULT_LAYOUTS


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("R128_DB_PATH", "r128_jobs.db")
    # Maximum raw PCM/WAV bytes accepted for a single job. Local fixtures only,
    # but a bound still prevents an accidental /dev/zero upload.
    max_job_bytes: int = _env_int("R128_MAX_JOB_BYTES", 256 * 1024 * 1024)
    allowed_channel_counts: tuple[int, ...] = (1, 2, 6)
    default_layouts: dict[int, list[str]] | None = None

    def __post_init__(self) -> None:
        if self.default_layouts is None:
            object.__setattr__(self, "default_layouts", dict(DEFAULT_LAYOUTS))


settings = Settings()
