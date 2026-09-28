"""Fixed kernel policies and the index-width ladder.

These constants define the supported range of the service; see README.
"""
from __future__ import annotations

# Index bit widths are independent of the validity bitmap: only positions
# marked valid can be dereferenced. Local indexes live in exactly one of
# these unsigned integer widths.
WIDTHS = (8, 16, 32, 64)

# Unsigned capacity: an 8-bit dictionary may hold codes 0..255 -> 256 values.
def capacity(width: int) -> int:
    if width not in WIDTHS:
        raise ValueError(f"width must be one of {WIDTHS}, got {width}")
    return 1 << width


# Fixed global dictionary ordering policy:
#   1. value TYPE (rank fixed here, so "1" string and 1 int never collide),
#   2. within a type: int64 by numeric value, UTF-8 by byte order of UTF-8.
# The policy is part of the persisted run metadata; changing it would change
# every code assignment, which is why it is fixed rather than configurable.
SORT_POLICY = "type_then_value"
TYPE_RANK = {"int64": 0, "utf8": 1}

WIDTH_POLICIES = ("reject", "expand")

# Sentinel stored in kernel arrays at NULL positions. It is NEVER dereferenced
# because the matching validity bit is 0. Local indexes are unsigned, so -1
# cannot be confused with a valid code.
NULL_SENTINEL = -1
