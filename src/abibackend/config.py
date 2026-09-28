"""Independent configuration layer.

Everything is resolvable from environment variables with local-safe defaults so
the service runs out of the box against synthetic fixtures and a local SQLite
file. Nothing here talks to a network or a production account.
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
    return default if raw is None or raw.strip() == "" else raw


@dataclass(frozen=True)
class Settings:
    # Where the indexed store lives. ":memory:" is honoured by sqlite3 but is
    # useless across connections; default to a local file in the workdir.
    db_path: str
    api_host: str
    api_port: int
    # Hard guard against malicious declared lengths/allocations during decode.
    max_alloc_bytes: int
    # A single word is 32 bytes; fixed array sizing also enforces a bound.
    max_words: int
    log_level: str
    replay_seed: int
    chain_id: int

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=_env_str("ABI_DB_PATH", os.path.join(os.getcwd(), "data", "abibackend.sqlite3")),
            api_host=_env_str("ABI_API_HOST", "127.0.0.1"),
            api_port=_env_int("ABI_API_PORT", 8080),
            # 8 MiB ceiling; canonical encodings we emit never approach it.
            max_alloc_bytes=_env_int("ABI_MAX_ALLOC_BYTES", 8 * 1024 * 1024),
            max_words=_env_int("ABI_MAX_WORDS", 1_000_000),
            log_level=_env_str("ABI_LOG_LEVEL", "INFO"),
            replay_seed=_env_int("ABI_REPLAY_SEED", 264),
            chain_id=_env_int("ABI_CHAIN_ID", 264),
        )


SETTINGS = Settings.from_env()
