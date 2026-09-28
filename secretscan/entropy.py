"""Entropy threshold primitive (Shannon entropy over token character sets).

Entropy here is deliberately simple and explainable: Shannon entropy in bits
per character of the exact matched value. It is a *shape* signal only. A high
score is neither necessary nor sufficient evidence of a secret — the scanner
requires a structural rule to fire as well, and the result is always a
candidate with a confidence level, never a confirmed leak.
"""

from __future__ import annotations

import collections
import math


def shannon_entropy(text: str) -> float:
    """Shannon entropy in bits/character of ``text`` (0 for empty input)."""
    if not text:
        return 0.0
    counts = collections.Counter(text)
    length = len(text)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())
