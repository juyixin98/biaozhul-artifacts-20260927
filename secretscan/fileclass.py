"""Text vs binary classification.

The distinction matters for reporting (line/column semantics differ) and for
the boundary statement in every report. The rule is deliberately simple and
deterministic: NUL bytes mean binary; otherwise the content must decode as
strict UTF-8.
"""

from __future__ import annotations

TEXT = "text"
BINARY = "binary"


def classify_bytes(data: bytes) -> str:
    if b"\x00" in data:
        return BINARY
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return BINARY
    return TEXT
