"""Text vs binary classification.

Classification rule (deterministic, explainable):

* A NUL byte anywhere marks the content **binary** — text formats essentially
  never contain NUL, while compressed/object/compiled files do.
* Otherwise the content must decode as strict UTF-8. Content that does not
  decode (e.g. latin-1 bytes, half-written files) is **binary** for scanning
  purposes; binary scanning only inspects printable-ASCII runs, so no bytes are
  misread as text.

Text files are scanned as text; binary files are scanned by extracting
printable-ASCII runs of at least ``min_run`` characters. Both paths apply the
same rule set, but the scan report records which path was used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Printable ASCII including space, tab; runs are trimmed later.
# Control characters that a real text file essentially never contains,
# excluding tab/newline/carriage-return/backspace/form-feed/vertical-tab.
_TEXT_CONTROL = set(range(0, 32)) - {8, 9, 10, 11, 12, 13}


@dataclass(frozen=True)
class MediaKind:
    kind: str  # "text" | "binary"
    reason: str


def classify(data: bytes) -> MediaKind:
    """Classify file content as ``text`` or ``binary`` with a stated reason."""
    if b"\x00" in data:
        return MediaKind("binary", "nul-byte-present")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return MediaKind("binary", "not-valid-utf8")
    # UTF-8 decoded fine; reject text that contains many binary control bytes
    # without NUL (rare, but keeps the boundary explicit).
    controls = sum(1 for b in data if b in _TEXT_CONTROL)
    if controls > 0 and controls / max(len(data), 1) > 0.05:
        return MediaKind("binary", "control-byte-ratio")
    return MediaKind("text", "utf8-decodable")


def printable_runs(data: bytes, min_run: int) -> list[tuple[bytes, int]]:
    """Return ``(run, byte_offset)`` for printable runs >= ``min_run``."""
    if min_run < 1:
        raise ValueError("min_run must be >= 1")
    pattern = re.compile(rb"[\x20-\x7e]{%d,}" % min_run)
    return [(m.group(0), m.start()) for m in pattern.finditer(data)]
