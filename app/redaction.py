"""Redaction helpers.

Raw user text is considered sensitive by default: logs and persisted
diagnostics never print it, only its length and a short SHA-256 fingerprint
(useful for correlating identical requests without revealing content).
"""
from __future__ import annotations

import hashlib

_MAX_REVEAL = 16


def redact_text(text: str, reveal: bool = False) -> str:
    """Return a safe description of ``text``.

    * ``reveal=False`` (default): ``"len=<n>,sha256=<8hex>"``;
    * ``reveal=True``: at most 16 characters plus the same fingerprint,
      intended solely for local debugging.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    if reveal:
        shown = text if len(text) <= _MAX_REVEAL else text[:_MAX_REVEAL] + "…"
        return f"len={len(text)},sha256={digest},preview={shown!r}"
    return f"len={len(text)},sha256={digest}"


def redact_surfaces(surfaces, reveal: bool = False) -> object:
    """Redact a sequence of token surfaces (kept off logs by default)."""
    surfaces = list(surfaces)
    if reveal:
        return surfaces
    return {"count": len(surfaces)}
