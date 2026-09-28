"""Runtime configuration.

All values are local-only: a fresh Fernet key is generated when none is
provided, the SQLite database lives under a local directory, and every
component uses synthetic data. Nothing here requires a production account.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache


def _new_fernet_key() -> str:
    # Imported lazily so importing config does not require cryptography.
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


@dataclass(frozen=True)
class Settings:
    db_path: str = field(
        default_factory=lambda: os.environ.get(
            "LOGSAFE_DB", "./var/audit.sqlite3"
        )
    )
    # Key for encrypting original fragments at rest. Generated per-process by
    # default (ephemeral): ciphertext from a previous process then cannot be
    # revealed. Set LOGSAFE_FERNET_KEY for a persistent local deployment.
    fernet_key: str = field(
        default_factory=lambda: os.environ.get(
            "LOGSAFE_FERNET_KEY", _new_fernet_key()
        )
    )
    # Shared secret guarding the audit read/reveal endpoints.
    audit_key: str = field(
        default_factory=lambda: os.environ.get(
            "LOGSAFE_AUDIT_KEY", "local-synthetic-audit-key"
        )
    )
    default_profile: str = field(
        default_factory=lambda: os.environ.get("LOGSAFE_PROFILE", "standard")
    )

    # Upper bound (in characters) of how much tail a streaming session keeps
    # uncommitted so that a sensitive fragment split across chunks can never be
    # partially emitted. Equal to the longest supported rule span.
    stream_holdback: int = 600


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def new_request_id() -> str:
    """Opaque request correlation id (no business data)."""
    return "req_" + secrets.token_hex(8)
