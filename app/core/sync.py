"""Transport packet layer: 188-byte packet framing and sync recovery.

Sync loss is recovered by a *bounded* scan, as required by the contract:
after losing lock the scanner searches forward for a run of sync bytes at the
packet period (188 bytes) rather than accepting the first 0x47 it sees.  The
search window is bounded (``max_scan_bytes``) and the number of skipped bytes
is reported, so recovery cost and diagnostics stay concrete.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SYNC_BYTE = 0x47
PACKET_SIZE = 188
# Maximum garbage run we are willing to scan through to reacquire lock.
DEFAULT_MAX_SCAN_BYTES = 10 * PACKET_SIZE
# A candidate lock must survive this many packet-period sync confirmations.
LOCK_CONFIRM_PACKETS = 2


@dataclass
class AdaptationField:
    length: int
    pcr: int | None = None          # 27MHz+90kHz units, or None
    discontinuity: bool = False
    random_access: bool = False
    raw: bytes | None = None


@dataclass
class TSPacket:
    """One parsed 188-byte transport packet (view over the source bytes)."""

    index: int                  # zero-based packet ordinal after lock
    byte_offset: int            # offset of the sync byte in the input
    payload_unit_start: bool    # PUSI
    pusi: bool                  # alias kept for readability at call sites
    pid: int
    tsc: int                    # transport_scrambling_control
    adaptation_field_control: int
    continuity_counter: int
    adaptation: AdaptationField | None
    payload: bytes
    raw: bytes

    @property
    def has_payload(self) -> bool:
        return self.adaptation_field_control in (0b01, 0b11)

    @property
    def has_adaptation(self) -> bool:
        return self.adaptation_field_control in (0b10, 0b11)

    @property
    def tei(self) -> bool:
        return bool(self.raw[1] & 0x80)


def _parse_adaptation(body: bytes) -> AdaptationField | None:
    """Parse the adaptation field (``body`` starts at the adaptation_length byte).

    Returns ``None`` if the field claims to extend beyond the packet
    (malformed); the caller then treats the whole packet as payload-less.
    """
    if not body:
        return None
    length = body[0]
    if length == 0:
        return AdaptationField(length=0)
    if length > len(body) - 1:
        return None
    flags = body[1] if length >= 1 else 0
    af = AdaptationField(
        length=length,
        discontinuity=bool(flags & 0x80),
        random_access=bool(flags & 0x40),
        raw=bytes(body[: 1 + length]),
    )
    if flags & 0x10 and length >= 7:  # PCR_flag
        base = (
            (body[2] << 25)
            | (body[3] << 17)
            | (body[4] << 9)
            | (body[5] << 1)
            | (body[6] >> 7)
        )
        ext = ((body[6] & 0x01) << 8) | body[7]
        af.pcr = base * 300 + ext
    return af


def parse_packet(buf: bytes, index: int, byte_offset: int) -> TSPacket | None:
    """Parse exactly 188 bytes beginning with a sync byte.

    Returns ``None`` for a structurally invalid packet (bad AFC or an
    adaptation field that overruns the packet boundary).
    """
    if len(buf) < PACKET_SIZE or buf[0] != SYNC_BYTE:
        return None
    b1, b2, b3 = buf[1], buf[2], buf[3]
    pusi = bool(b1 & 0x40)
    pid = ((b1 & 0x1F) << 8) | b2
    tsc = (b3 >> 6) & 0x03
    afc = (b3 >> 4) & 0x03
    cc = b3 & 0x0F
    if afc == 0b00:  # reserved for future use by ISO/IEC 13818-1
        return None

    adaptation: AdaptationField | None = None
    payload_start = 4
    if afc in (0b10, 0b11):
        adaptation = _parse_adaptation(buf[4:PACKET_SIZE])
        if adaptation is None:
            return None
        payload_start = 4 + 1 + adaptation.length
        if afc == 0b11 and payload_start > PACKET_SIZE:
            return None

    payload = b""
    if afc in (0b01, 0b11):
        payload = bytes(buf[payload_start:PACKET_SIZE])
    return TSPacket(
        index=index,
        byte_offset=byte_offset,
        payload_unit_start=pusi,
        pusi=pusi,
        pid=pid,
        tsc=tsc,
        adaptation_field_control=afc,
        continuity_counter=cc,
        adaptation=adaptation,
        payload=payload,
        raw=bytes(buf[:PACKET_SIZE]),
    )


@dataclass
class ScanEvent:
    """Result of one scanner iteration."""

    kind: str            # "packet" | "resync" | "trailing"
    packet: TSPacket | None = None
    skipped_bytes: int = 0
    from_offset: int = 0
    to_offset: int = 0
    trailing_bytes: int = 0


class PacketScanner:
    """Frame an arbitrary byte buffer into 188-byte packets with sync recovery.

    Yields :class:`ScanEvent` objects rather than raising on corruption:

    * ``packet``   -- one in-lock packet
    * ``resync``   -- lock was lost and recovered (``skipped_bytes`` garbage
                      bytes were jumped over); bounded by ``max_scan_bytes``
    * ``trailing`` -- input ended with fewer than 188 bytes after lock, or
                      with garbage the bounded scan could not recover
    """

    def __init__(
        self,
        data: bytes | bytearray | memoryview,
        max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
    ) -> None:
        self.data = bytes(data)
        self.max_scan_bytes = max_scan_bytes

    def _find_lock(self, start: int) -> int | None:
        """Vectorized bounded search for a confirmed 188-byte-period lock."""
        data = self.data
        end = min(len(data), start + self.max_scan_bytes + 1)
        if end - start < PACKET_SIZE:
            # Fall back to a single-sync check for short tails.
            off = data.find(SYNC_BYTE, start, end)
            return off if off >= 0 else None
        window = np.frombuffer(data[start:end], dtype=np.uint8)
        syncs = np.flatnonzero(window == SYNC_BYTE)
        for cand in syncs:
            c = int(cand)
            ok = True
            for k in range(1, LOCK_CONFIRM_PACKETS + 1):
                p = c + k * PACKET_SIZE
                if p >= len(data) or data[p] != SYNC_BYTE:
                    ok = False
                    break
            if ok:
                return start + c
        # Nothing with two confirmations; accept a lone sync if a whole packet
        # fits (tail packets cannot be period-confirmed).
        off = data.find(SYNC_BYTE, start, min(len(data), start + self.max_scan_bytes + 1))
        if off >= 0 and off + PACKET_SIZE <= len(data):
            return off
        return None

    def events(self):
        data = self.data
        n = len(data)
        pos = self._find_lock(0)
        if pos is None:
            if n:
                yield ScanEvent(kind="trailing", from_offset=0, to_offset=n,
                                trailing_bytes=n)
            return
        if pos > 0:
            yield ScanEvent(kind="resync", skipped_bytes=pos,
                            from_offset=0, to_offset=pos)

        index = 0
        while pos + PACKET_SIZE <= n:
            if data[pos] != SYNC_BYTE:
                new_pos = self._find_lock(pos)
                if new_pos is None:
                    skipped = n - pos
                    yield ScanEvent(kind="trailing", skipped_bytes=skipped,
                                    from_offset=pos, to_offset=n,
                                    trailing_bytes=skipped)
                    return
                yield ScanEvent(kind="resync",
                                skipped_bytes=new_pos - pos,
                                from_offset=pos, to_offset=new_pos)
                pos = new_pos
                continue

            pkt = parse_packet(data[pos:pos + PACKET_SIZE], index, pos)
            if pkt is None:
                # Structurally bad packet at a locked position: do not trust
                # the lock; attempt bounded recovery from the next byte.
                new_pos = self._find_lock(pos + 1)
                if new_pos is None:
                    yield ScanEvent(kind="trailing",
                                    skipped_bytes=n - pos,
                                    from_offset=pos, to_offset=n,
                                    trailing_bytes=n - pos)
                    return
                yield ScanEvent(kind="resync",
                                skipped_bytes=new_pos - pos,
                                from_offset=pos, to_offset=new_pos)
                pos = new_pos
                continue

            yield ScanEvent(kind="packet", packet=pkt)
            index += 1
            pos += PACKET_SIZE

        if pos < n:
            yield ScanEvent(kind="trailing", from_offset=pos, to_offset=n,
                            trailing_bytes=n - pos)
