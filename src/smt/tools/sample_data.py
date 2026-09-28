"""Deterministic synthetic fixture: keys with a *long shared prefix*.

All keys are 256-bit hex; values are synthetic local strings.
"""
from __future__ import annotations

from typing import List, Tuple

# k_a and k_b share a 248-bit prefix (first 31 bytes identical), differing
# only in the 249th key bit. k_c diverges from them at bit 0.
FIXTURE: List[Tuple[str, str]] = [
    ("00" * 30 + "ab" + "00", "alpha"),
    ("00" * 30 + "ab" + "01", "beta"),
    ("ff" * 32, "gamma"),
]

# Keys used by proof tests but *not* inserted (non-membership witnesses):
ABSENT_IN_SUBTREE = "00" * 30 + "ab" + "02"   # shares the 248-bit prefix
ABSENT_ELSEWHERE = "11" * 32                   # diverges at bit 0
