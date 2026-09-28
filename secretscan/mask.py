"""Irreversible masking of matched secret material.

Only the masked representation is ever stored or rendered. The masking keeps a
short prefix/suffix so humans can correlate two findings, but never enough to
reconstruct the value.
"""

from __future__ import annotations

KEEP_PREFIX = 4
KEEP_SUFFIX = 2
ELLIPSIS = "…"


def mask_value(raw: bytes | str) -> str:
    """Return a stable, irreversible mask for ``raw``.

    Rules (verified by unit tests):
      * len <= 4  -> first char + ellipsis
      * len 5..7  -> prefix(2)…suffix(1)
      * len >= 8  -> prefix(4)…suffix(2)
    """
    if isinstance(raw, str):
        data = raw
    else:
        data = raw.decode("latin-1")
    n = len(data)
    if n == 0:
        return ELLIPSIS
    if n <= 4:
        return data[:1] + ELLIPSIS
    if n <= 7:
        return data[:2] + ELLIPSIS + data[-1:]
    return data[:KEEP_PREFIX] + ELLIPSIS + data[-KEEP_SUFFIX:]
