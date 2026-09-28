"""Runtime configuration.

Every value has a safe local default; each can be overridden with an
environment variable named ``TSANALYZER_<FIELD>`` (e.g.
``TSANALYZER_DB_PATH=/tmp/x.db``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # SQLite database used by the job store.
    db_path: str = "data/tsanalyzer.db"
    # Max bytes scanned ahead when looking for a sync byte.
    max_sync_scan_bytes: int = 4096
    # Consecutive 0x47 (at 188-byte spacing) required to declare sync lock.
    sync_confirm_packets: int = 3
    # Maximum accepted size of one reassembled PSI section.
    max_section_bytes: int = 4096
    # Maximum payload bytes buffered for one reassembled PES ("受限" cap).
    max_pes_payload_bytes: int = 256 * 1024
    # Maximum bytes consumed while trying to locate a PES header.
    max_pes_header_bytes: int = 512
    # Maximum accepted upload / in-memory input size.
    max_input_bytes: int = 64 * 1024 * 1024
    # Max number of events embedded in a report JSON (remainder stay in DB).
    report_event_limit: int = 500
    # Worker threads executing queued analysis jobs.
    job_workers: int = 2
    # Max number of completed jobs kept before the oldest are purged.
    max_jobs: int = 128

    @classmethod
    def from_env(cls) -> "Settings":
        kwargs: dict[str, object] = {}
        for field_name in cls.__dataclass_fields__:  # type: ignore[attr-defined]
            env_name = "TSANALYZER_" + field_name.upper()
            raw = os.environ.get(env_name)
            if raw is None:
                continue
            current = getattr(cls, field_name)
            if isinstance(current, bool):
                kwargs[field_name] = raw.strip().lower() in {"1", "true", "yes", "on"}
            elif isinstance(current, int):
                kwargs[field_name] = int(raw)
            else:
                kwargs[field_name] = raw
        return cls(**kwargs)


DEFAULT_SETTINGS = Settings.from_env()
