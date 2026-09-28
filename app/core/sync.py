"""Byte-stream framing with finite-scan sync recovery.

Sync strategy (bounded, not "scan forever"):

1. Vectorised scan for candidate 0x47 bytes with NumPy.
2. Confirm a candidate by checking 0x47 at +188 and +376
   (``sync_confirm_packets`` boundaries).
3. If no candidate confirms within ``max_sync_scan_bytes``, recovery
   fails and the job is reported as such instead of silently guessing.

After lock, every packet is verified individually; a bad sync byte in
the middle of a stream triggers a bounded rescan (not an unbounded one).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .packets import SYNC_BYTE, TS_PACKET_SIZE


@dataclass(frozen=True)
class SyncSearchResult:
    offset: Optional[int]      # absolute offset of confirmed packet, or None
    scanned_bytes: int         # bytes examined while searching
    skipped_bytes: int         # bytes skipped relative to the search start


def sync_candidate_offsets(
    data: bytes | bytearray | memoryview, start: int, end: int
) -> np.ndarray:
    """Absolute offsets in ``[start, end)`` holding a 0x47 byte.

    Uses a vectorised NumPy comparison; this is the bulk hot path when a
    stream is preceded by arbitrary garbage.
    """
    if end <= start:
        return np.empty(0, dtype=np.int64)
    window = np.frombuffer(data, dtype=np.uint8, count=end - start, offset=start)
    return (np.nonzero(window == SYNC_BYTE)[0] + start).astype(np.int64)


def confirm_sync(
    data: bytes | bytearray | memoryview,
    offset: int,
    confirm_packets: int = 3,
) -> bool:
    """Check sync bytes at ``offset`` plus ``confirm_packets - 1`` packet steps."""
    for step in range(confirm_packets):
        pos = offset + step * TS_PACKET_SIZE
        if pos >= len(data) or data[pos] != SYNC_BYTE:
            return False
    return True


def find_sync(
    data: bytes | bytearray | memoryview,
    start: int = 0,
    max_scan_bytes: int = 4096,
    confirm_packets: int = 3,
) -> SyncSearchResult:
    """Bounded search for a confirmed TS packet boundary.

    Scans at most ``max_scan_bytes`` of garbage ahead of ``start``.
    Candidates are vectorised, but confirmation still verifies real
    packet structure (repeated sync bytes at the 188-byte period).
    """
    scan_end = min(len(data), start + max_scan_bytes)
    candidates = sync_candidate_offsets(data, start, scan_end)

    for candidate in candidates:
        if candidate + (confirm_packets - 1) * TS_PACKET_SIZE >= len(data):
            # Not enough following bytes to confirm; keep looking.
            continue
        if confirm_sync(data, int(candidate), confirm_packets):
            return SyncSearchResult(
                offset=int(candidate),
                scanned_bytes=int(candidate) - start + 1,
                skipped_bytes=int(candidate) - start,
            )

    return SyncSearchResult(
        offset=None,
        scanned_bytes=max(0, scan_end - start),
        skipped_bytes=0,
    )
