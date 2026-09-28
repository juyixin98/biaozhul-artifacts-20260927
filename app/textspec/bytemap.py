"""UTF-8 byte/codepoint indexing and range validation.

The whole service reasons in **raw byte offsets** into the normalized UTF-8
buffer.  UTF-8 is designed so that leading vs continuation bytes can be
classified by two bits, which lets every operation here stay linear and
allocation-light:

* byte ``b`` is a continuation byte iff ``b & 0b1100_0000 == 0b1000_0000``;
* every other byte begins a codepoint (or is ASCII).

:class:`ByteIndex` caches the list of codepoint-start offsets.  That is one
``array('I')`` of (#codepoints + 1) entries -- 4 bytes per codepoint, ~40 MiB
per 10 M codepoints -- instead of a Python ``list`` of ints (~280 MiB).
"""

from __future__ import annotations

import bisect
import hashlib
from array import array

from ..errors import InvalidByteRangeError


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_boundary(data: bytes, pos: int) -> bool:
    """True if ``pos`` is 0, ``len(data)``, or a UTF-8 leading-byte position."""
    if pos <= 0:
        return pos == 0
    if pos >= len(data):
        return pos == len(data)
    return (data[pos] & 0xC0) != 0x80


def next_codepoint_boundary(data: bytes, pos: int) -> int | None:
    """Smallest codepoint-start offset strictly greater than ``pos``.

    Returns ``None`` at/after end of buffer.  This is the engine's zero-width
    "advance one position" rule: we skip an *entire* codepoint, never a single
    raw byte (RE2's raw-bytes scanner would otherwise restart mid-codepoint).
    """
    n = len(data)
    if pos < 0:
        pos = 0
    j = pos + 1
    while j < n and (data[j] & 0xC0) == 0x80:
        j += 1
    return j if j <= n else None


class ByteIndex:
    """Codepoint-start offsets of a UTF-8 buffer."""

    __slots__ = ("data", "_starts", "_codepoints")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self._starts = self._build(data)
        # _starts: offset 0, every subsequent leading byte, then len(data);
        # hence #interior boundaries == #codepoints.
        self._codepoints = len(self._starts) - 1

    @staticmethod
    def _build(data: bytes) -> array:
        starts: array = array("I")
        append = starts.append
        append(0)
        for i in range(len(data)):
            if (data[i] & 0xC0) != 0x80:
                if i != 0:
                    append(i)
        append(len(data))
        return starts

    @property
    def codepoints(self) -> int:
        return self._codepoints

    @property
    def starts(self) -> array:
        return self._starts

    def is_aligned(self, pos: int) -> bool:
        """``pos`` coincides with a codepoint boundary (0 and EOF included)."""
        idx = bisect.bisect_left(self._starts, pos)
        return idx < len(self._starts) and self._starts[idx] == pos

    def advance(self, pos: int) -> int | None:
        idx = bisect.bisect_right(self._starts, pos)
        if idx >= len(self._starts):
            return None
        return int(self._starts[idx])

    def validate_range(self, start: int, end: int) -> None:
        """Raise :class:`InvalidByteRangeError` unless [start, end) is legal."""
        validate_byte_range(self.data, start, end, index=self)


def validate_byte_range(
    data: bytes,
    start: int,
    end: int,
    *,
    index: ByteIndex | None = None,
) -> None:
    """Check 0 <= start <= end <= len(data) and UTF-8 boundary alignment.

    A half-open range that starts and ends on codepoint boundaries covers whole
    codepoints (this includes empty ranges on a boundary -- zero-width matches).
    """
    n = len(data)
    if not isinstance(start, int) or not isinstance(end, int):
        raise InvalidByteRangeError(
            "range endpoints must be integers", start=start, end=end
        )
    if start < 0 or end < 0:
        raise InvalidByteRangeError("range endpoints must be >= 0", start=start, end=end)
    if start > n or end > n:
        raise InvalidByteRangeError(
            "range exceeds source length", start=start, end=end, length=n
        )
    if start > end:
        raise InvalidByteRangeError("range start must be <= end", start=start, end=end)

    if index is not None:
        aligned = index.is_aligned(start) and index.is_aligned(end)
    else:
        aligned = is_boundary(data, start) and is_boundary(data, end)
    if not aligned:
        raise InvalidByteRangeError(
            "range endpoints do not align to UTF-8 codepoint boundaries",
            start=start,
            end=end,
        )

    # Whole-codepoint integrity.  Alignment of both ends proves it for
    # non-empty ranges in valid UTF-8: a leading byte begins a well-formed
    # sequence that ends exactly at the next boundary, and strict-decoding the
    # slice guards against malformed/truncated sequences.  An empty range at a
    # boundary decodes to "" trivially.
    try:
        data[start:end].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise InvalidByteRangeError(
            "range does not cover whole UTF-8 codepoints",
            start=start,
            end=end,
            reason=exc.reason,
        ) from exc
