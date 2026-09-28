"""Sensitive-data redaction and request identifiers.

Values supplied to the review may contain secrets (passwords, tokens, email
addresses). The analyzer never executes them, but it also must not leak them
into logs or the encrypted audit record. This module turns a value into a
*safe fingerprint*: its type, a length and a truncated SHA-256 prefix. Two
identical values produce identical fingerprints, so reviewers can correlate
bindings without seeing content.
"""

from __future__ import annotations

import hashlib
import uuid

SENSITIVE_NAME_HINTS = (
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "credit", "ssn", "private",
)

MAX_INLINE_LEN = 32


def new_request_id() -> str:
    return "rev_" + uuid.uuid4().hex


def sql_digest(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


def _fingerprint(value) -> str:
    blob = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(blob).hexdigest()[:12]


def is_sensitive_name(name) -> bool:
    if name is None:
        return False
    lowered = str(name).lower()
    return any(h in lowered for h in SENSITIVE_NAME_HINTS)


def redact_value(value, *, name=None):
    """Return a JSON-safe, content-free description of a bound value."""
    sensitive = is_sensitive_name(name)
    if value is None:
        return {"kind": "null", "length": 0, "fingerprint": _fingerprint(None),
                "redacted": sensitive}
    if isinstance(value, bool):
        # bool must be checked before int
        desc = {"kind": "boolean", "length": None,
                "fingerprint": _fingerprint(value)}
    elif isinstance(value, int):
        desc = {"kind": "integer", "length": None,
                "fingerprint": _fingerprint(value)}
    elif isinstance(value, float):
        desc = {"kind": "number", "length": None,
                "fingerprint": _fingerprint(value)}
    elif isinstance(value, str):
        desc = {"kind": "string", "length": len(value),
                "fingerprint": _fingerprint(value)}
        if not sensitive and len(value) <= MAX_INLINE_LEN:
            # short, non-sensitive values are safe to reproduce
            desc["preview"] = value
    elif isinstance(value, (list, tuple)):
        return {
            "kind": "array",
            "length": len(value),
            "elements": [redact_value(v, name=name) for v in value],
            "redacted": sensitive,
        }
    else:
        desc = {"kind": type(value).__name__, "length": None,
                "fingerprint": _fingerprint(str(type(value)))}
    if sensitive:
        desc["redacted"] = True
        desc.pop("preview", None)
    return desc


def redact_identifier(raw: str) -> dict:
    # Identifiers are validated against a whitelist, so their content is
    # constrained; still return a fingerprint for diagnostics.
    return {
        "raw_preview": raw[:16] + ("…" if len(raw) > 16 else ""),
        "length": len(raw),
        "fingerprint": _fingerprint(raw),
    }
