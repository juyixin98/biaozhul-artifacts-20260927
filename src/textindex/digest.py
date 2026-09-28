"""Content digest used to bind an index version to its source text."""

from __future__ import annotations

import hashlib


def digest_utf8(data: bytes) -> str:
    """SHA-256 hex digest of the canonical text's UTF-8 bytes."""
    return hashlib.sha256(data).hexdigest()
