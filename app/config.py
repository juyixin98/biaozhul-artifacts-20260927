"""Process configuration.

Intentionally dependency-free (read from environment) so the security kernel
and state layers can be configured and tested without any web framework.
"""
from __future__ import annotations

import os
import dataclasses


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclasses.dataclass(frozen=True)
class Settings:
    """Runtime settings.

    ``db_path`` uses a local SQLite file (synthetic/local deployment only).
    ``audit_to_stderr`` additionally mirrors *fingerprint-only* audit events to
    standard error for operators; it never prints secrets or share values.
    """

    db_path: str = "data/shamir.db"
    audit_to_stderr: bool = True
    # Hard safety cap so a request cannot ask for absurdly large fan-outs.
    max_total_shares: int = 64
    # galois falls back to a pure-Python field implementation when numba/llvmlite
    # are absent. Force that here for deterministic, JIT-free local behaviour.
    force_pure_python_fields: bool = True

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("SHAMIR_DB_PATH", "data/shamir.db"),
            audit_to_stderr=_as_bool(os.environ.get("SHAMIR_AUDIT_STDERR"), True),
            max_total_shares=int(os.environ.get("SHAMIR_MAX_TOTAL_SHARES", "64")),
            force_pure_python_fields=_as_bool(
                os.environ.get("SHAMIR_PURE_PY_FIELDS"), True
            ),
        )
