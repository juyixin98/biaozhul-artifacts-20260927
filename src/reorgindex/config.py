"""Runtime configuration loaded from environment variables with safe defaults.

No production accounts or external services are used: every default points at a
local SQLite file.  See config/test.env and config/dev.env for examples used by
the test-suite and the local demo service respectively.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "run" / "reorgindex.db"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    return int(raw)


@dataclass(frozen=True)
class Settings:
    database_path: Path
    # Finality depth K: a block with more than K confirmations (i.e. at least
    # K+1 blocks of work above it, itself included) is final.  A switch that
    # would need to detach such a block is rejected.
    finality_depth: int
    # Consensus-permitted per-block weights (a block's weight equals its
    # declared difficulty).  Regular synthetic blocks use 4; weighted blocks
    # use 16 so a short fork can legitimately out-weigh a longer one.
    allowed_difficulties: frozenset[int]
    service_name: str
    log_level: str

    @staticmethod
    def from_env() -> "Settings":
        raw = _env("REORG_ALLOWED_DIFFICULTIES", "4,16")
        allowed = frozenset(int(part) for part in raw.split(",") if part.strip())
        return Settings(
            database_path=Path(_env("REORG_DB_PATH", str(DEFAULT_DB))),
            finality_depth=_env_int("REORG_FINALITY_DEPTH", 3),
            allowed_difficulties=allowed,
            service_name=_env("REORG_SERVICE_NAME", "reorg-derived-index"),
            log_level=_env("REORG_LOG_LEVEL", "INFO").upper(),
        )
