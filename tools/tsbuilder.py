"""Independent synthetic MPEG-TS fixture builder.

NOTHING in this module imports from ``app.core``: it is an independent
reference implementation of just enough of ISO/IEC 13818-1 to synthesize
streams, and expected results (gaps, duplicates, program maps, PES extents)
are tracked here by construction.  Tests therefore assert against
builder-derived ground truth rather than against values produced by the
system under test.
"""
from __future__ import annotations

from dataclasses import dataclass

PACKET_SIZE = 188
SYNC_BYTE = 0x47
PAT_PID = 0x0000


# ---------------------------------------------------------------------------
# Independent CRC32/MPEG-2 (same polynomial as the parser, separately coded)
# ---------------------------------------------------------------------------
def crc32_mpeg2(data: bytes) -> int:
    table: list[int] = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if c & 0x80000000 \
                else (c << 1) & 0xFFFFFFFF
        table.append(c)
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) ^ b) & 0xFF]
    return crc


# ---------------------------------------------------------------------------
# TS packets
# ---------------------------------------------------------------------------
def ts_packet(pid: int, payload: bytes = b"", *, pusi: bool = False,
              cc: int = 0, adaptation: bytes | None = None,
              tei: bool = False, pcr: int | None = None,
              discontinuity: bool = False, random_access: bool = False,
              scrambling: int = 0) -> bytes:
    """Build one 188-byte packet.

    ``payload`` must fit after the 4-byte header and any adaptation field.
    """
    b1 = (0x80 if tei else 0) | (0x40 if pusi else 0) | (0x10 if pid > 255 else 0) | (pid >> 8)
    b2 = pid & 0xFF
    has_flags = (adaptation is not None or pcr is not None
                 or discontinuity or random_access)
    afc = 0b01 if payload else 0b10
    af_bytes = b""
    body = b""
    if has_flags:
        afc = 0b11 if payload else 0b10
        flags = (0x80 if discontinuity else 0) | \
                (0x40 if random_access else 0) | \
                (0x10 if pcr is not None else 0)
        body_b = bytearray([flags])
        if pcr is not None:
            base, ext = divmod(pcr, 300)
            body_b += bytes([
                (base >> 25) & 0xFF, (base >> 17) & 0xFF,
                (base >> 9) & 0xFF, (base >> 1) & 0xFF,
                ((base & 1) << 7) | (ext >> 8), ext & 0xFF])
        if adaptation is not None:
            body_b += adaptation
        body = bytes(body_b)
    elif not payload:
        # Adaptation-only with no flags: still emit adaptation_length=0.
        afc = 0b10
        body = b""
    if afc in (0b10, 0b11):
        af_bytes = bytes([len(body)]) + body
    b3 = (scrambling << 6) | (afc << 4) | (cc & 0x0F)
    pkt = bytearray([SYNC_BYTE, b1, b2, b3]) + af_bytes + payload
    if len(pkt) > PACKET_SIZE:
        raise ValueError(f"packet payload too large: {len(pkt)} bytes")
    pkt += b"\xFF" * (PACKET_SIZE - len(pkt))
    return bytes(pkt)


# ---------------------------------------------------------------------------
# PSI sections
# ---------------------------------------------------------------------------
def pat_section(programs: dict[int, int], *, version: int = 1,
                tsid: int = 0x0001, section_number: int = 0,
                last_section: int = 0, network_pid: int | None = None,
                current_next: int = 1) -> bytes:
    body = bytearray()
    if network_pid is not None:
        body += (0).to_bytes(2, "big")
        body += (0xE000 | network_pid).to_bytes(2, "big")
    for prog, pid in sorted(programs.items()):
        body += prog.to_bytes(2, "big")
        body += (0xE000 | pid).to_bytes(2, "big")
    head = bytearray(8)
    head[0] = 0x00  # table_id
    length = 5 + len(body) + 4
    head[1] = 0xB0 | (length >> 8)
    head[2] = length & 0xFF
    head[3:5] = tsid.to_bytes(2, "big")
    head[5] = 0xC1 | (version << 1) | (current_next & 1)
    head[6] = section_number
    head[7] = last_section
    section = bytes(head) + bytes(body)
    return section + crc32_mpeg2(section).to_bytes(4, "big")


def pmt_section(program_number: int, pmt_pid: int,
                streams: list[tuple[int, int]], *, version: int = 1,
                pcr_pid: int | None = None, section_number: int = 0,
                last_section: int = 0) -> bytes:
    """``streams`` is a list of (stream_type, elementary_pid)."""
    if pcr_pid is None and streams:
        pcr_pid = streams[0][1]
    pcr_pid = pcr_pid or 0x1FFF
    loop = bytearray()
    for stype, epid in streams:
        loop += bytes([stype])
        loop += (0xE000 | epid).to_bytes(2, "big")
        loop += b"\xF0\x00"  # ES_info_length = 0
    head = bytearray(12)
    head[0] = 0x02
    length = 5 + 4 + len(loop) + 4  # after sec len: 5 hdr + pcr+pil + loop + crc
    head[1] = 0xB0 | (length >> 8)
    head[2] = length & 0xFF
    head[3:5] = program_number.to_bytes(2, "big")
    head[5] = 0xC1 | (version << 1)
    head[6] = section_number
    head[7] = last_section
    head[8:10] = (0xE000 | pcr_pid).to_bytes(2, "big")
    head[10:12] = (0xF000 | 0).to_bytes(2, "big")  # program_info_length=0
    section = bytes(head) + bytes(loop)
    return section + crc32_mpeg2(section).to_bytes(4, "big")


def corrupt_crc(section: bytes) -> bytes:
    """Flip one bit in the CRC field so the section fails verification."""
    b = bytearray(section)
    b[-1] ^= 0x01
    return bytes(b)


# ---------------------------------------------------------------------------
# Section packetization (with explicit split points for cross-packet tests)
# ---------------------------------------------------------------------------
def section_packets(pid: int, section: bytes, *,
                    start_cc: int = 0,
                    split_at: list[int] | None = None) -> list[bytes]:
    """Packetize one section across 1..n TS packets.

    The first packet carries pointer_field=0.  ``split_at`` gives the
    section-byte offset at which each subsequent packet starts; by default
    the section fills as much of each 184-byte payload as it can.
    """
    packets: list[bytes] = []
    cc = start_cc
    if split_at is None:
        # First packet: 1 pointer + up to 183 section bytes.
        chunks = [section[:183]]
        rest = section[183:]
        while rest:
            chunks.append(rest[:184])
            rest = rest[184:]
    else:
        # ``split_at`` forces extra packet boundaries at the given
        # section-byte offsets.  Offsets are given relative to the section
        # start; the first segment may be at most 183 bytes (it shares its
        # TS packet with pointer_field).  A short segment produces 0xFF TS
        # padding at the END of that packet, which the next (non-PUSI)
        # packet skips by resuming the section at its first payload byte.
        bounds = [0] + list(split_at) + [len(section)]
        chunks: list[bytes] = []
        for i in range(len(bounds) - 1):
            chunks.append(section[bounds[i]:bounds[i + 1]])
    first = True
    for chunk in chunks:
        is_first = first
        payload = (b"\x00" if is_first else b"") + chunk
        payload = payload + b"\xFF" * (184 - len(payload))
        packets.append(ts_packet(pid, payload, pusi=is_first, cc=cc))
        first = False
        cc = (cc + 1) % 16
    return packets


# ---------------------------------------------------------------------------
# PES
# ---------------------------------------------------------------------------
def pes_packet(stream_id: int, payload: bytes, *, pts: int | None = None,
               dts: int | None = None, unbounded: bool = False) -> bytes:
    opt = bytearray()
    flag = 0x80  # PES_scrambling=0, priority=0, alignment=0
    if pts is not None and dts is None:
        flag |= 0x80  # PTS_DTS_flags = 10
        def ts_bytes(ts: int, tag4: int) -> bytes:
            return bytes([
                tag4 | ((ts >> 30) & 0x07) << 1 | 1,
                (ts >> 22) & 0xFF,
                ((ts >> 14) & 0x7F) << 1 | 1,
                (ts >> 7) & 0xFF,
                ((ts & 0x7F) << 1) | 1,
            ])
        opt += ts_bytes(pts, 0x20)
    elif pts is not None and dts is not None:
        flag |= 0xC0
        def ts_bytes(ts: int, tag4: int) -> bytes:
            return bytes([
                tag4 | ((ts >> 30) & 0x07) << 1 | 1,
                (ts >> 22) & 0xFF,
                ((ts >> 14) & 0x7F) << 1 | 1,
                (ts >> 7) & 0xFF,
                ((ts & 0x7F) << 1) | 1,
            ])
        opt += ts_bytes(pts, 0x30)
        opt += ts_bytes(dts, 0x10)
    head = bytes([0x80, flag, len(opt)]) + bytes(opt)
    body = head + payload
    length = 0 if unbounded else len(body)
    return b"\x00\x00\x01" + bytes([stream_id]) + length.to_bytes(2, "big") + body


def pes_packets(pid: int, pes: bytes, *, start_cc: int = 0) -> list[bytes]:
    """Fragment one PES across TS packets (first has PUSI, rest continuation).

    Intermediate packets are filled completely with PES bytes.  Only the
    final packet is 0xFF-padded (TS stuffing), which the analyzer trims using
    the PES packet_length, so fragments are never over-assembled.
    """
    out: list[bytes] = []
    cc = start_cc
    first = True
    rest = pes
    while True:
        cap = 184
        chunk = rest[:cap]
        rest = rest[cap:]
        is_last = not rest
        payload = chunk + (b"\xFF" * (cap - len(chunk)) if is_last else b"")
        out.append(ts_packet(pid, payload, pusi=first, cc=cc))
        cc = (cc + 1) % 16
        first = False
        if is_last:
            break
    return out


# ---------------------------------------------------------------------------
# Ground-truth tracking stream builder
# ---------------------------------------------------------------------------
@dataclass
class ExpectedPes:
    pid: int
    start_packet_index: int
    length: int
    pts: int | None
    dts: int | None


class StreamBuilder:
    """Accumulates packets while tracking expected per-PID CC state.

    Expected anomalies are recorded explicitly: tests compare the analyzer's
    findings against the builder's ledger, never against the analyzer's own
    counters.
    """

    def __init__(self) -> None:
        self.buf = bytearray()
        self.cc: dict[int, int] = {}
        self.packet_count = 0
        # pid -> list of packet indexes carrying payload
        self.expected_pes: list[ExpectedPes] = []
        # ledger entries: (kind, pid, packet_index, details)
        self.ledger: list[tuple[str, int, int, dict]] = []

    def add(self, pkt: bytes, pid: int, *, advances_cc: bool = True,
            duplicate_of: bytes | None = None, note: str | None = None) -> int:
        idx = self.packet_count
        self.buf += pkt
        self.packet_count += 1
        if duplicate_of is not None:
            self.ledger.append(("duplicate_packet", pid, idx,
                                {"note": note}))
        return idx

    def add_payload_packet(self, pid: int, payload: bytes, *,
                           pusi: bool = False, cc: int | None = None,
                           tei: bool = False, pcr: int | None = None,
                           discontinuity: bool = False,
                           adaptation: bytes | None = None,
                           auto_cc: bool = True) -> int:
        if auto_cc:
            cc = self.cc.get(pid, cc if cc is not None else 0)
        assert cc is not None
        pkt = ts_packet(pid, payload, pusi=pusi, cc=cc, tei=tei, pcr=pcr,
                        discontinuity=discontinuity, adaptation=adaptation)
        idx = self.add(pkt, pid)
        if auto_cc:
            self.cc[pid] = (cc + 1) % 16
        return idx

    def add_duplicate(self, pid: int, original: bytes) -> int:
        idx = self.add(original, pid, duplicate_of=original)
        return idx

    def add_raw(self, data: bytes) -> None:
        """Insert bytes without touching CC state (garbage / resync tests)."""
        self.buf += data

    def add_gap(self, pid: int, missing: int, *, signaled: bool = False,
                next_payload: bytes = b"", pusi: bool = False) -> int:
        """Simulate `missing` lost packets; next packet's CC jumps.

        With ``signaled=True`` the next packet's adaptation carries the
        discontinuity indicator. Returns the index of the arrival packet.
        """
        last = self.cc.get(pid, 0)
        arrived_cc = (last + missing + 1) % 16 if self.cc.get(pid) is not None \
            else missing % 16
        pkt = ts_packet(pid, next_payload, pusi=pusi, cc=arrived_cc,
                        discontinuity=signaled)
        idx = self.add(pkt, pid)
        self.ledger.append((
            "signaled_discontinuity" if signaled else "cc_gap",
            pid, idx, {"missing_estimate": missing}))
        self.cc[pid] = (arrived_cc + 1) % 16
        return idx

    def build(self) -> bytes:
        return bytes(self.buf)
