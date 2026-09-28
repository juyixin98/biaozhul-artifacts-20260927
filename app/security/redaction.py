"""Boundary helpers: what is allowed to appear in public material.

Public material = API responses, disclosure proofs, audit-log detail payloads.
It must never contain salts (of undisclosed *or* disclosed fields beyond the
exact single disclosed proof), undisclosed raw values, or private batch
records. These helpers are used both by the serializers and by a test that
asserts no secret leaks into public artifacts.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

_KEY_PATTERNS = (
    re.compile(r"^salt$"),
    re.compile(r"salt_hex$"),
    re.compile(r"^raw_value$"),
    re.compile(r"_secret$"),
)


def key_is_secret(key: str) -> bool:
    return any(p.search(key) for p in _KEY_PATTERNS)


def assert_no_secret_markers(obj: Any, path: str = "$") -> Iterable[str]:
    """Walk a JSON-able object; yield paths that carry secret-looking keys.

    Note: disclosed proofs intentionally carry the single revealed salt under
    ``salt_hex`` -- callers scope this check to public/listing artifacts, not
    to a disclosure proof for a field the owner chose to reveal.
    """
    leaks: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if key_is_secret(str(k)):
                leaks.append(f"{path}.{k}")
            leaks.extend(assert_no_secret_markers(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            leaks.extend(assert_no_secret_markers(v, f"{path}[{i}]"))
    return leaks


def fingerprint(value: Any, digest_name: str = "sha256", length: int = 16) -> str:
    """Short non-reversible correlation id for audit logs (hex truncation)."""
    from app.security.hashing import labeled_hash  # local: avoid cycle at import

    payload = repr(value).encode("utf-8", errors="replace")
    return labeled_hash("audit-commit-v1|correlation", digest_name, payload).hex()[
        :length
    ]
