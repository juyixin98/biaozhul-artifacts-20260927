"""Bidirectional byte / codepoint / grapheme-cluster index.

We build the index ourselves (the mature library only supplies cluster
segmentation).  For every grapheme cluster we record where it starts in all
three position spaces:

* ``cluster_index``  — 0..N,           number of clusters before this point
* ``cp_start[i]``    — codepoint offset of cluster i's first code point
* ``byte_start[i]``  — UTF-8 byte offset of cluster i's first code point

Each table has length N+1, the last entry being the end of text.  Because a
cluster boundary is always a codepoint boundary which is always a byte
boundary, "is this offset a legal boundary?" becomes a membership test —
edits inside a combining sequence or mid-UTF-8 are rejected categorically.

The arrays are packed into a versioned, checksummed blob for SQLite storage
(:func:`pack`/:func:`unpack`).  The canonical text itself is never in the
blob; the row binds blob ⇄ text via a SHA-256 digest.
"""

from __future__ import annotations

import array
import hashlib
import struct
from dataclasses import dataclass

from . import segmenter
from .errors import (
    IllegalByteBoundary,
    IllegalCodepointBoundary,
    IllegalGraphemeBoundary,
    IndexCorrupt,
    IndexVersionMismatch,
    PositionOutOfRange,
)
from .unicode_version import (
    BLOB_FORMAT_VERSION,
    DATA_VERSION_IDENTITY,
    UNICODE_VERSION,
)

#: array typecode: signed 64-bit (covers byte/cp/cluster offsets up to 2**63).
_OFFSET_TYPE = "q"

_MAGIC = b"TXIDX001"
# header: magic(8) format(H) unicode_len(H) identity_len(H) sha256(32) count(I)
_HEADER = struct.Struct(">8sHHH32sI")
_U64 = struct.Struct(">Q")


@dataclass(frozen=True)
class TextIndex:
    """Immutable index over one canonical text.

    ``text`` is attached for in-process lookups but is not part of the
    serialized blob; a deserialized index is rebound to its row's text.
    """

    text: str
    cp_start: array.array
    byte_start: array.array
    unicode_version: str = UNICODE_VERSION

    # --- sizes -------------------------------------------------------------

    @property
    def cluster_count(self) -> int:
        """Number of clusters (table length minus the end sentinel)."""
        return len(self.cp_start) - 1

    @property
    def codepoint_count(self) -> int:
        return self.cp_start[-1]

    @property
    def byte_count(self) -> int:
        return self.byte_start[-1]

    # --- boundary predicates ----------------------------------------------

    def is_cluster_index(self, cluster: int) -> bool:
        return 0 <= cluster <= self.cluster_count

    def is_codepoint_boundary(self, cp: int) -> bool:
        return cp in self._cp_set

    def is_byte_boundary(self, byte: int) -> bool:
        return byte in self._byte_set

    # --- forward: cluster index → other spaces ----------------------------

    def cluster_to_codepoint(self, cluster: int) -> int:
        self._require_cluster(cluster)
        return self.cp_start[cluster]

    def cluster_to_byte(self, cluster: int) -> int:
        self._require_cluster(cluster)
        return self.byte_start[cluster]

    # --- reverse / cross-space --------------------------------------------

    def codepoint_to_cluster(self, cp: int, *, strict: bool = True) -> int:
        """Cluster starting at codepoint offset ``cp``.

        strict=True (default): offset must be a cluster boundary.
        strict=False: return the cluster containing the offset.
        """
        if not 0 <= cp <= self.codepoint_count:
            raise PositionOutOfRange("codepoint", cp, self.codepoint_count)
        if strict and cp not in self._cp_set:
            raise IllegalCodepointBoundary(cp)
        return self._floor_le(self.cp_start, cp)

    def byte_to_cluster(self, byte: int, *, strict: bool = True) -> int:
        """Cluster starting at UTF-8 byte offset ``byte``.

        strict=True: offset must be a UTF-8 lead boundary *and* a cluster
        boundary.  A byte offset inside a multi-byte codepoint raises
        :class:`IllegalByteBoundary`; a lead byte at a codepoint that is not
        a cluster start (e.g. the combining mark in ``e\\u0301``) raises
        :class:`IllegalCodepointBoundary`.
        strict=False: any valid lead byte is accepted (the containing
        cluster is returned); continuation bytes still rejected.
        """
        if not 0 <= byte <= self.byte_count:
            raise PositionOutOfRange("byte", byte, self.byte_count)
        if byte not in self._lead_set:
            raise IllegalByteBoundary(byte)
        if strict and byte not in self._byte_set:
            # Codepoint lead but not a cluster start; report the codepoint
            # offset without going through byte_to_codepoint (which itself
            # calls this method — avoid mutual recursion).
            cluster = self._floor_le(self.byte_start, byte)
            cp = self.cp_start[cluster] + self._codepoints_before_in_cluster(
                cluster, byte - self.byte_start[cluster])
            raise IllegalCodepointBoundary(cp)
        return self._floor_le(self.byte_start, byte)

    def byte_to_codepoint(self, byte: int, *, strict: bool = True) -> int:
        """Codepoint whose UTF-8 encoding begins at byte offset ``byte``."""
        cluster = self.byte_to_cluster(byte, strict=strict)
        return self.cp_start[cluster] + self._codepoints_before_in_cluster(
            cluster, byte - self.byte_start[cluster]
        )

    def codepoint_to_byte(self, cp: int, *, strict: bool = True) -> int:
        """UTF-8 byte offset where codepoint ``cp`` is encoded."""
        cluster = self.codepoint_to_cluster(cp, strict=strict)
        if strict:
            return self.byte_start[cluster]
        within_cp = cp - self.cp_start[cluster]
        prefix = self._cluster_text(cluster)[:within_cp].encode("utf-8")
        return self.byte_start[cluster] + len(prefix)

    # --- introspection -----------------------------------------------------

    def cluster_text(self, cluster: int) -> str:
        self._require_cluster(cluster)
        return self._cluster_text(cluster)

    def clusters(self) -> list[str]:
        return [
            self.text[self.cp_start[i]:self.cp_start[i + 1]]
            for i in range(self.cluster_count)
        ]

    # --- internals ----------------------------------------------------------

    @property
    def _cp_set(self) -> set[int]:
        return set(self.cp_start)

    @property
    def _byte_set(self) -> set[int]:
        return set(self.byte_start)

    @property
    def _lead_set(self) -> set[int]:
        return _utf8_lead_offsets(self.byte_count, self.text)

    def _codepoints_before_in_cluster(self, cluster: int, byte_offset: int) -> int:
        """Count cluster codepoints whose UTF-8 starts precede ``byte_offset``."""
        if cluster == self.cluster_count:
            return 0  # end sentinel: no cluster text, offset must be 0
        encoded = self._cluster_text(cluster).encode("utf-8")
        return _count_cps_before(encoded, byte_offset)

    def _cluster_text(self, cluster: int) -> str:
        return self.text[self.cp_start[cluster]:self.cp_start[cluster + 1]]

    def _require_cluster(self, cluster: int) -> None:
        if not 0 <= cluster <= self.cluster_count:
            raise IllegalGraphemeBoundary(cluster, self.cluster_count)

    @staticmethod
    def _floor_le(sorted_starts: array.array, value: int) -> int:
        """Index of the greatest start <= value (bisect_right - 1)."""
        lo, hi = 0, len(sorted_starts)
        while lo < hi:
            mid = (lo + hi) // 2
            if sorted_starts[mid] <= value:
                lo = mid + 1
            else:
                hi = mid
        return lo - 1


def _utf8_seq_len(lead: int) -> int:
    if lead < 0x80:
        return 1
    if lead < 0xE0:
        return 2
    if lead < 0xF0:
        return 3
    return 4


def _count_cps_before(encoded: bytes, byte_offset: int) -> int:
    i = cps = 0
    while i < byte_offset:
        i += _utf8_seq_len(encoded[i])
        cps += 1
    return cps


def _utf8_lead_offsets(byte_count: int, text: str) -> set[int]:
    """All UTF-8 lead-byte offsets (prefix sums of codepoint widths)."""
    offsets: set[int] = {0, byte_count}
    pos = 0
    for ch in text:
        offsets.add(pos)
        pos += len(ch.encode("utf-8"))
    return offsets


# --- construction ----------------------------------------------------------


def build_index(text: str, *, max_clusters: int | None = None) -> TextIndex:
    """Build the full index over canonical, surrogate-free ``text``."""
    spans = segmenter.cluster_spans(text)
    if max_clusters is not None and len(spans) > max_clusters:
        from .errors import TooManyClusters
        raise TooManyClusters(len(spans), max_clusters)

    cp_start = array.array(_OFFSET_TYPE, [0])
    byte_start = array.array(_OFFSET_TYPE, [0])
    byte_pos = 0
    cp_pos = 0
    for s, e in spans:
        # spans cover [0,len(text)] contiguously; encode each cluster once.
        byte_pos += len(text[s:e].encode("utf-8"))
        cp_pos = e
        byte_start.append(byte_pos)
        cp_start.append(cp_pos)
    idx = TextIndex(text=text, cp_start=cp_start, byte_start=byte_start)
    _validate_invariants(idx, text)
    return idx


def rebuild(text: str, *, max_clusters: int | None = None) -> TextIndex:
    """Plain full rebuild — the reference against which edits are compared."""
    return build_index(text, max_clusters=max_clusters)


def _validate_invariants(idx: TextIndex, text: str) -> None:
    if len(idx.cp_start) != len(idx.byte_start):
        raise IndexCorrupt("table length mismatch",
                           cp=len(idx.cp_start), byte=len(idx.byte_start))
    if idx.cp_start[0] != 0 or idx.byte_start[0] != 0:
        raise IndexCorrupt("first start must be 0")
    if idx.cp_start[-1] != len(text):
        raise IndexCorrupt("last cp start != codepoint count")
    if idx.byte_start[-1] != len(text.encode("utf-8")):
        raise IndexCorrupt("last byte start != byte count")
    for arr in (idx.cp_start, idx.byte_start):
        for a, b in zip(arr, arr[1:]):
            if b <= a:
                raise IndexCorrupt("starts must be strictly increasing")


# --- serialization ---------------------------------------------------------


def pack(idx: TextIndex) -> bytes:
    """Serialize offsets (not the text) into a versioned, checksummed blob."""
    identity = f"blob{BLOB_FORMAT_VERSION}:{DATA_VERSION_IDENTITY}".encode()
    count = len(idx.cp_start)  # N+1 entries
    body_cp = idx.cp_start.tobytes()
    body_by = idx.byte_start.tobytes()
    payload = (
        _U64.pack(count)
        + body_cp
        + body_by
    )
    checksum = hashlib.sha256(payload).digest()
    header = _HEADER.pack(
        _MAGIC,
        BLOB_FORMAT_VERSION,
        int(UNICODE_VERSION.split(".")[0]),
        len(identity),
        checksum,
        count,
    )
    return header + identity + payload


def unpack(blob: bytes, *, text: str) -> TextIndex:
    """Deserialize and validate; rebind to the stored canonical ``text``."""
    header_size = _HEADER.size
    if len(blob) < header_size:
        raise IndexCorrupt("blob shorter than header", size=len(blob))
    magic, fmt, _uni_major, identity_len, checksum, count = _HEADER.unpack(
        blob[:header_size]
    )
    if magic != _MAGIC:
        raise IndexCorrupt("bad magic", magic=magic.hex())
    if fmt != BLOB_FORMAT_VERSION:
        raise IndexVersionMismatch(f"blob-format-v{fmt}",
                                   f"blob-format-v{BLOB_FORMAT_VERSION}")
    identity = blob[header_size:header_size + identity_len].decode(
        "utf-8", errors="replace"
    )
    expected_identity = f"blob{BLOB_FORMAT_VERSION}:{DATA_VERSION_IDENTITY}"
    if identity != expected_identity:
        raise IndexVersionMismatch(identity, expected_identity)

    payload = blob[header_size + identity_len:]
    if hashlib.sha256(payload).digest() != checksum:
        raise IndexCorrupt("payload checksum mismatch")

    expected_payload_len = _U64.size + 2 * count * 8
    if len(payload) != expected_payload_len:
        raise IndexCorrupt(
            "payload length mismatch",
            actual=len(payload), expected=expected_payload_len,
        )
    count_in_payload = _U64.unpack(payload[:_U64.size])[0]
    if count_in_payload != count:
        raise IndexCorrupt("count disagreement between header and payload")

    cp = array.array(_OFFSET_TYPE)
    by = array.array(_OFFSET_TYPE)
    cp.frombytes(payload[_U64.size:_U64.size + count * 8])
    by.frombytes(payload[_U64.size + count * 8:])
    idx = TextIndex(text=text, cp_start=cp, byte_start=by)
    try:
        _validate_invariants(idx, text)
    except IndexCorrupt:
        raise
    except Exception as exc:  # malformed arrays must never surface raw
        raise IndexCorrupt(f"invariant validation failed: {exc}") from None
    return idx
