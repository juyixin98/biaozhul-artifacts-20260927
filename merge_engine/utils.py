"""Small deterministic helpers shared across modules."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

MISSING = object()


def quote_ident(name: str) -> str:
    """Double-quote an SQLite identifier safely (``a"b`` -> ``""a""b""``)."""
    return '"' + name.replace('"', '""') + '"'


def canonical_json(value: Any) -> str:
    """JSON dump with sorted keys and fixed separators - stable for hashes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": value.hex()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


def fingerprint(rows: Iterable[Any]) -> str:
    """Order-independent SHA-256 over a collection of row-like values."""
    parts = sorted(canonical_json(r) for r in rows)
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def sort_token(value: Any) -> Any:
    """Totally-ordered, NULL-aware key token for mixed-type row values.

    Ordering: NULLs first, then booleans/numbers, then strings, then
    JSON-serialized complex values. Used only to make duplicate scans and
    action sequences deterministic - it never affects matching equality.
    """
    if value is None:
        return (0, 0, 0)
    if isinstance(value, bool):
        return (1, int(value), 0)
    if isinstance(value, (int, float)):
        return (1, float(value), 0)
    if isinstance(value, str):
        return (2, value, 0)
    return (3, canonical_json(value), 0)


def key_sort_token(key: Iterable[Any]) -> tuple:
    return tuple(sort_token(v) for v in key)


def key_group_token(key: Iterable[Any]) -> Any:
    """Grouping token for "equal keys" under NULLS NOT DISTINCT.

    NULL becomes a sentinel that equals only itself (None already does in
    tuples/hashes, and cannot collide with a string); other values pass
    through. Lists -> tuples so the token is hashable.
    """
    out = []
    for v in key:
        if isinstance(v, (list, tuple)):
            v = tuple(v)
        out.append(v)
    return tuple(out)


def short_hash() -> str:
    """3 random bytes hex (6 chars) for run-id suffixes."""
    import secrets

    return secrets.token_hex(3)
