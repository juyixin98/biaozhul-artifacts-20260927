"""Application configuration, driven entirely by environment variables.

No production accounts or external services are referenced; every knob has a
local, offline-safe default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    service_name: str = "r128-backend"
    # Identifier echoed into every result/log so outputs are traceable to the
    # algorithm revision (BS.1770-4 K-weight + R128 absolute/relative gating).
    algorithm_id: str = "ebu-r128-bs1770-4-tech3341-3342-v1"
    algorithm_spec_refs: tuple[str, ...] = (
        "EBU R128 v2020",
        "ITU-R BS.1770-4",
        "EBU Tech 3341 (momentary/integrated block rules)",
        "EBU Tech 3342 (LRA)",
    )
    # Identity of this processing location/worker; overridden in tests.
    worker_id: str = field(default_factory=lambda: os.environ.get("R128_WORKER_ID", "local-worker"))
    db_path: str = field(default_factory=lambda: os.environ.get("R128_DB_PATH", "./r128_jobs.db"))
    max_payload_bytes: int = field(
        default_factory=lambda: int(os.environ.get("R128_MAX_PAYLOAD_BYTES", str(64 * 1024 * 1024)))
    )
    # R128 timing constants (seconds), kept configurable for the test suite.
    momentary_block_sec: float = 0.4
    momentary_hop_sec: float = 0.1
    shortterm_block_sec: float = 3.0
    shortterm_hop_sec: float = 0.1
    absolute_gate_lufs: float = -70.0
    integrated_relative_offset_lu: float = -10.0
    lra_relative_offset_lu: float = -20.0
    # Tech 3342 analyses at least ~30 s of material for a confident LRA; below
    # this many short-term blocks the result is still reported but flagged.
    lra_confident_block_count: int = 30
    supported_sample_rates: tuple[int, ...] = (8000, 12000, 16000, 22050, 24000, 32000,
                                               44100, 48000, 88200, 96000, 192000)


def get_settings() -> Settings:
    return Settings()
