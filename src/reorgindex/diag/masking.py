"""Redaction helpers for diagnostics.

Private/sensitive material must never be written to the diagnostics log or to
stdout.  Block hashes, transaction ids and addresses are synthetic-chain
identifiers but are still truncated by default because logs are often shared;
the full value is kept *only* in the structured DB record columns, where it is
needed for review.
"""
from __future__ import annotations

import re

_ADDRESS = re.compile(r"^(rx1[0-9a-f]{40})$")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")

# Fields that must never appear in a log line in clear text.  Signatures and
# public keys alone do not move funds in this synthetic chain, but they are
# treated as sensitive anyway to make the rule simple and auditable.
SENSITIVE_KEYS = {"signature", "pow_signature", "pubkey", "producer", "private_key"}


def mask_hash(value: str, keep: int = 12) -> str:
    if not isinstance(value, str) or len(value) <= keep + 2:
        return "<redacted>"
    return f"{value[:keep]}…"


def mask_address(value: str) -> str:
    if not isinstance(value, str):
        return "<redacted>"
    if _ADDRESS.match(value):
        return f"{value[:8]}…{value[-4:]}"
    return "<redacted>"


def mask_secret(value: object) -> str:
    return "<redacted:secret>"


def redact(obj: object) -> object:
    """Return a copy of ``obj`` safe to print."""
    if isinstance(obj, dict):
        out: dict[str, object] = {}
        for key, value in obj.items():
            if key in SENSITIVE_KEYS or "private" in str(key):
                out[key] = mask_secret(value)
            elif key in ("sender", "recipient", "fee_recipient", "address"):
                out[key] = mask_address(str(value))
            elif key in ("txid", "hash", "parent", "block_hash", "active_tip", "new_tip"):
                out[key] = mask_hash(str(value)) if value else value
            else:
                out[key] = redact(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj
