"""Application configuration.

All settings are read from environment variables with the ``DICUNIFY_`` prefix.
Nothing here talks to a real external system; the SQLite path points at a local
file by default.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Value types supported by the dictionary encoding service.
VALUE_TYPES: frozenset[str] = frozenset({"string", "int64", "double", "bool"})

# Unsigned Arrow index types selected per cardinality tier.
# The chosen width is the first whose capacity is >= cardinality.
# Width 8 additionally needs at least one distinct value (see _auto_width).
AUTO_WIDTHS: tuple[int, ...] = (8, 16, 32)

# Capacity of an unsigned index of each bit width.
WIDTH_CAPACITY: dict[int, int] = {
    8: 2**8 - 1,
    16: 2**16 - 1,
    32: 2**32 - 1,
}

#: Maximum cardinality the service will ever unify (uint32 ceiling).
DEFAULT_MAX_CARDINALITY: int = 2**32 - 1

#: Fixed dictionary ordering policy identifier exposed to clients and persisted
#: in every job. Changing this string is a breaking metadata change.
SORT_POLICY: str = "typed-ascending-v1"


@dataclass(frozen=True)
class Settings:
    db_path: Path
    max_cardinality: int
    log_level: str

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> "Settings":
        env = {**os.environ, **(env or {})}
        raw = env.get("DICUNIFY_DB_PATH", "data/dicunify.db")
        max_card_raw = env.get("DICUNIFY_MAX_CARDINALITY", str(DEFAULT_MAX_CARDINALITY))
        try:
            max_card = int(max_card_raw)
        except ValueError as exc:
            raise ValueError(
                f"DICUNIFY_MAX_CARDINALITY must be an int, got {max_card_raw!r}"
            ) from exc
        if not 1 <= max_card <= DEFAULT_MAX_CARDINALITY:
            raise ValueError(
                "DICUNIFY_MAX_CARDINALITY must be within [1, 2^32-1]; "
                f"got {max_card}"
            )
        level = env.get("DICUNIFY_LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError(f"unknown DICUNIFY_LOG_LEVEL: {level!r}")
        return Settings(db_path=Path(raw), max_cardinality=max_card, log_level=level)
