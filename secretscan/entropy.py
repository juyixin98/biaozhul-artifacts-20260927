"""Shannon entropy heuristic.

Entropy is *one* signal, never a verdict. Tokens that clear the threshold but
match no structural rule are reported as uncertain, low-confidence heuristic
candidates with the reason ``entropy_only``.
"""

from __future__ import annotations

import math
import re
from collections import Counter

# ASCII secret-like runs. Deliberately ASCII-only so that non-UTF-8 / binary
# content can be scanned the same way as text.
DEFAULT_TOKEN_RE = rb"[A-Za-z0-9+/=_.\-]{%d,}"


def shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def token_pattern(min_length: int) -> re.Pattern[bytes]:
    return re.compile(DEFAULT_TOKEN_RE % min_length)


def iter_token_spans(content: bytes, min_length: int):
    """Yield ``(start, end, token_bytes)`` for secret-like ASCII runs."""
    for m in token_pattern(min_length).finditer(content):
        yield m.start(), m.end(), m.group(0)
