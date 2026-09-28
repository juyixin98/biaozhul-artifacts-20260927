"""Service configuration.  Values come from environment variables so tests
and local runs can override them without touching code."""

from __future__ import annotations

import os
from dataclasses import dataclass

SERVICE_NAME = "mp4-timeline-service"
SERVICE_VERSION = "0.1.0"


@dataclass(frozen=True)
class Settings:
    db_path: str = "./mp4timeline_jobs.db"
    max_file_bytes: int = 512 * 1024 * 1024  # refuse larger inputs up front

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("MP4TL_DB_PATH", "./mp4timeline_jobs.db"),
            max_file_bytes=int(os.environ.get("MP4TL_MAX_FILE_BYTES", str(512 * 1024 * 1024))),
        )
