"""环境变量配置。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    data_dir: str
    max_samples_per_job: int
    max_chunk_bytes: int
    max_jobs: int
    event_ring: int

    @staticmethod
    def from_env() -> "Settings":
        def get_int(name: str, default: int) -> int:
            raw = os.environ.get(name)
            if raw is None or raw == "":
                return default
            try:
                v = int(raw)
                assert v > 0
                return v
            except (ValueError, AssertionError) as e:
                raise RuntimeError(f"invalid env {name}={raw!r}") from e

        return Settings(
            data_dir=os.environ.get("SEG_DATA_DIR", "./data"),
            max_samples_per_job=get_int("SEG_MAX_SAMPLES_PER_JOB", 8_000_000),
            max_chunk_bytes=get_int("SEG_MAX_CHUNK_BYTES", 8 * 1024 * 1024),
            max_jobs=get_int("SEG_MAX_JOBS", 200),
            event_ring=get_int("SEG_EVENT_RING", 2000),
        )
