"""Process configuration.

Values come from environment variables prefixed with ``MTSA_`` (e.g.
``MTSA_DB_PATH`` overrides ``db_path``).  The configuration object is
immutable per process; tests construct their own ``Settings`` instances.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/jobs.db"
    max_upload_bytes: int = 50 * 1024 * 1024
    # Constrained PES reassembly: any single reassembled PES longer than this
    # is rejected rather than buffered without bound.
    max_pes_bytes: int = 256 * 1024
    # Cap on buffered bytes of a PSI section (section_length is at most 4093
    # per the standard, so anything beyond this is malformed).
    max_section_bytes: int = 4096
    worker_count: int = 1

    @staticmethod
    def from_env(prefix: str = "MTSA_") -> "Settings":
        kwargs: dict[str, object] = {}
        int_fields = {"max_upload_bytes", "max_pes_bytes",
                      "max_section_bytes", "worker_count"}
        for key, value in os.environ.items():
            if not key.startswith(prefix):
                continue
            name = key[len(prefix):].lower()
            if name in int_fields:
                kwargs[name] = int(value)
            else:
                kwargs[name] = value
        return Settings(**kwargs)  # type: ignore[arg-type]

    def ensure_dirs(self) -> None:
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
