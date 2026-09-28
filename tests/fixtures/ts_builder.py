"""Independent synthetic MPEG-TS fixture builder.

This module is *independent* of ``app.core``: it contains its own CRC-32
implementation (bitwise, not table-driven), its own packet/section/PES
construction code, and scenarios with hand-specified expectations. The
analyzer and its test oracle therefore cannot share a bug.

Everything here builds legal (or deliberately broken, when a scenario
asks for it) 188-byte transport streams from integer literals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

PACKET_SIZE = 188
SYNC = 0x47

# Stream / PID conventions used by the fixtures. These are fixture-side
# literals, not analyzer constants: tests assert the analyzer discovers them.
PAT_PID = 0x0000
PMT_PID = 0x0100
PMT_PID_V2 = 0x0110
VIDEO_PID = 0x0101
AUDIO_PID = 0x0102
VIDEO_TYPE = 0x1B  # H.264 video
AUDIO_TYPE = 0x0F  # AAC audio
PROGRAM_NUMBER = 1
TSID = 42

_NO_OPTIONAL_PES_HEADER = frozenset({0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF})

AFC_PAYLOAD_ONLY = 1
AFC_ADAPTATION_ONLY = 2



# ------------------------------------------------------------------ CRC
def mpeg_crc32_bitwise(data: bytes) -> int:
    """Independent MPEG-2 CRC-32: direct bitwise register simulation."""
    crc = 0xFFFFFFFF
    for byte in data:
        for shift in range(7, -1, -1):
            input_bit = (byte >> shift) & 1
            top_bit = (crc >> 31) & 1
            crc = (crc << 1) & 0xFFFFFFFF
            if top_bit ^ input_bit:
                crc ^= 0x04C11DB7
    return crc


# -------------------------------------------------------------- sections
def _section(table_id: int, payload: bytes, table_id_ext: int,
             version: int, current_next: bool,
             section_number: int = 0, last_section: int = 0,
             corrupt_crc: bool = False) -> bytes:
    after_length = (
        table_id_ext.to_bytes(2, "big")
        + bytes([(version << 1) | (1 if current_next else 0),
                 section_number, last_section])
        + payload
        + b"\x00\x00\x00\x00"  # CRC placeholder
    )
    section_length = len(after_length)  # includes placeholder CRC
    head = bytes([table_id, 0xB0 | (section_length >> 8), section_length & 0xFF])
    body = head + after_length
    crc = mpeg_crc32_bitwise(body[:-4])
    if corrupt_crc:
        crc ^= 0xDEADBEEF
    return body[:-4] + crc.to_bytes(4, "big")


def pat_section(programs: dict[int, int], *, version: int = 0,
                current_next: bool = True, tsid: int = TSID,
                corrupt_crc: bool = False) -> bytes:
    """Build a PAT section mapping program_number -> PMT PID."""
    entries = b"".join(
        pnum.to_bytes(2, "big") + bytes([0xE0 | (pid >> 8), pid & 0xFF])
        for pnum, pid in sorted(programs.items())
    )
    return _section(0x00, entries, tsid, version, current_next,
                    corrupt_crc=corrupt_crc)


def pmt_section(program_number: int, streams: list[tuple[int, int]], *,
                pcr_pid: int = 0x1FFF, version: int = 0,
                current_next: bool = True, corrupt_crc: bool = False) -> bytes:
    """Build a PMT section. streams = ordered list of (stream_type, pid)."""
    body = (
        bytes([0xE0 | (pcr_pid >> 8), pcr_pid & 0xFF])
        + b"\xF0\x00"  # program_info_length = 0
        + b"".join(
            bytes([stype, 0xE0 | (pid >> 8), pid & 0xFF, 0xF0, 0x00])
            for stype, pid in streams
        )
    )
    return _section(0x02, body, program_number, version, current_next,
                    corrupt_crc=corrupt_crc)


# --------------------------------------------------------------- packets
class CCGen:
    """Per-PID 4-bit continuity counter with manual control when needed."""

    def __init__(self):
        self._cc: dict[int, int] = {}

    def next(self, pid: int, has_payload: bool = True) -> int:
        if pid not in self._cc:
            value = 0
        else:
            value = (self._cc[pid] + 1) & 0xF if has_payload else self._cc[pid]
        self._cc[pid] = value
        return value

    def peek(self, pid: int) -> int:
        return self._cc.get(pid, 0)

    def set(self, pid: int, value: int) -> None:
        self._cc[pid] = value & 0xF


def encode_pcr(pcr_base: int, pcr_ext: int = 0) -> bytes:
    value = pcr_base * 300 + pcr_ext
    base, ext = value // 300, value % 300
    return bytes([
        (base >> 25) & 0xFF,
        (base >> 17) & 0xFF,
        (base >> 9) & 0xFF,
        (base >> 1) & 0xFF,
        ((base & 1) << 7) | ((ext >> 8) & 1),
        ext & 0xFF,
    ])


def ts_packet(pid: int, payload: bytes = b"", *, cc: int, pusi: bool = False,
              tei: bool = False, scramble: int = 0,
              pcr: Optional[tuple[int, int]] = None,
              discontinuity: bool = False,
              adaptation_extra: bytes = b"",
              force_afc: Optional[int] = None) -> bytes:
    """Construct one 188-byte packet. Raises if declared fields overrun."""
    af_body = b""
    flags = 0
    if discontinuity:
        flags |= 0x80
    if pcr is not None:
        flags |= 0x10
    if flags or adaptation_extra:
        af_body = bytes([flags])
        if pcr is not None:
            af_body += encode_pcr(pcr[0], pcr[1])
        af_body += adaptation_extra

    has_af = bool(af_body) or force_afc in (2, 3)
    has_payload = bool(payload) or force_afc in (1, 3)
    if force_afc is not None:
        afc = force_afc
    else:
        afc = (2 if has_af else 0) | (1 if has_payload else 0)

    b1 = (0x80 if tei else 0) | (0x40 if pusi else 0) | ((pid >> 8) & 0x1F)
    b2 = pid & 0xFF
    b3 = ((scramble << 6) & 0xC0) | ((afc << 4) & 0x30) | (cc & 0x0F)
    packet = bytearray(bytes([SYNC, b1, b2, b3]))

    if afc in (2, 3):
        # 184 bytes after the 4-byte header: length byte + af body + rest
        if afc == AFC_ADAPTATION_ONLY:
            stuffing = 184 - 1 - len(af_body)
            if stuffing < 0:
                raise ValueError("adaptation field overruns packet")
            packet += bytes([len(af_body)]) + af_body + b"\xFF" * stuffing
        else:
            payload_room = 184 - 1 - len(af_body)
            if len(payload) > payload_room:
                raise ValueError(
                    f"payload {len(payload)} > available {payload_room} on PID {pid:#x}"
                )
            packet += bytes([len(af_body)]) + af_body
    if afc in (1, 3):
        room = 184 if afc == AFC_PAYLOAD_ONLY else 184 - 1 - len(af_body)
        if len(payload) > room:
            raise ValueError(f"payload {len(payload)} > available {room}")
        packet += payload + b"\xFF" * (room - len(payload))

    if len(packet) != PACKET_SIZE:
        raise AssertionError(f"builder produced {len(packet)}-byte packet")
    return bytes(packet)



def packetize_section(pid: int, section: bytes, cc: CCGen) -> list[bytes]:
    """Place one PSI section into packets (pointer_field=0 on the start packet).

    Uses full 184-byte payload packets; the short final packet carries its
    stuffing in an adaptation field (afc=3) so payload length equals the
    remaining section bytes. This mirrors how real multiplexers emit PSI.
    """
    packets: list[bytes] = []
    pos = 0
    first = True
    while pos < len(section):
        if first:
            room = min(183, len(section) - pos)  # reserve pointer_field byte
            piece = b"\x00" + section[pos:pos + room]
            pos += room
        else:
            room = min(184, len(section) - pos)
            piece = section[pos:pos + room]
            pos += room
        last = pos >= len(section)
        if last and len(piece) < 184:
            packets.append(
                ts_packet(pid, piece, cc=cc.next(pid), pusi=first, force_afc=3)
            )
        else:
            packets.append(ts_packet(pid, piece, cc=cc.next(pid), pusi=first))
        first = False
    return packets


def packetize_raw_psi(pid: int, psi_bytes: bytes, cc: CCGen, *,
                      first_section_bytes: int = 183) -> list[bytes]:
    """Packetize PSI bytes forcing a cross-packet section continuation.

    The PUSI packet carries the pointer byte plus exactly
    ``first_section_bytes`` section bytes, then fills the rest of its 184
    payload with the following section bytes (i.e. it is a normal full
    payload packet). Continuation packets are full 184; only the final
    packet is short and uses adaptation-field stuffing (afc=3). Payload
    regions therefore never contain ambiguous 0xFF stuffing bytes.
    """
    assert 1 <= first_section_bytes <= 183
    packets: list[bytes] = []
    pos = 0
    first_piece = b"\x00" + psi_bytes[pos:pos + first_section_bytes]
    pos += first_section_bytes
    first_cap = 184  # total payload size of the first packet
    head_fill = min(184 - len(first_piece), len(psi_bytes) - pos)
    first_piece += psi_bytes[pos:pos + head_fill]
    pos += head_fill
    packets.append(ts_packet(pid, first_piece, cc=cc.next(pid), pusi=True))
    while pos < len(psi_bytes):
        piece = psi_bytes[pos:pos + 184]
        pos += 184
        last = pos >= len(psi_bytes)
        packets.append(
            ts_packet(pid, piece, cc=cc.next(pid), pusi=False,
                      force_afc=3 if last and len(piece) < 184 else None)
        )
    return packets


# Backwards-compatible alias kept for readability at call sites.
packetize_section_forced_split = packetize_raw_psi


# ------------------------------------------------------------------- PES
def encode_pts_tag(tag: int, ts90k: int) -> bytes:
    return bytes([
        (tag << 4) | (((ts90k >> 30) & 0x07) << 1) | 0x01,
        (ts90k >> 22) & 0xFF,
        (((ts90k >> 15) & 0xFF) << 1) | 0x01,
        (ts90k >> 7) & 0xFF,
        ((ts90k & 0x7F) << 1) | 0x01,
    ])


def pes_packet(stream_id: int, payload: bytes, *, pts: Optional[int] = None,
               dts: Optional[int] = None,
               pes_length: Optional[int] = None) -> bytes:
    if stream_id in _NO_OPTIONAL_PES_HEADER:
        length = len(payload) if pes_length is None else pes_length
        return b"\x00\x00\x01" + bytes([stream_id]) + length.to_bytes(2, "big") + payload

    header_data = b""
    pts_dts_flag = 0
    if pts is not None and dts is not None:
        pts_dts_flag = 3
        header_data = encode_pts_tag(3, pts) + encode_pts_tag(1, dts)
    elif pts is not None:
        pts_dts_flag = 2
        header_data = encode_pts_tag(2, pts)
    declared = pes_length
    if declared is None:
        declared = 3 + len(header_data) + len(payload)
    head = (
        b"\x00\x00\x01"
        + bytes([stream_id])
        + declared.to_bytes(2, "big")
        + bytes([0x80, pts_dts_flag << 6, len(header_data)])
        + header_data
    )
    return head + payload


def packetize_pes(pid: int, pes: bytes, cc: CCGen) -> list[bytes]:
    """Realistic PES packetization: full 184-byte payload packets, with the
    final partial packet stuffed through its adaptation field (afc=3) so the
    payload region carries exactly the remaining PES bytes."""
    packets: list[bytes] = []
    pos = 0
    first = True
    while pos < len(pes):
        piece = pes[pos:pos + 184]
        last = pos + 184 >= len(pes)
        if last and len(piece) < 184:
            packets.append(
                ts_packet(pid, piece, cc=cc.next(pid), pusi=first, force_afc=3)
            )
        else:
            packets.append(ts_packet(pid, piece, cc=cc.next(pid), pusi=first))
        first = False
        pos += 184
    return packets


# -------------------------------------------------------------- scenarios
@dataclass
class Scenario:
    name: str
    data: bytes
    expectation: dict = field(default_factory=dict)


def _base_program(pmt_pid: int = PMT_PID, video_pid: int = VIDEO_PID,
                  audio_pid: int = AUDIO_PID, pcr_pid: int = VIDEO_PID,
                  pat_version: int = 0, pmt_version: int = 0,
                  pat_current: bool = True, pmt_current: bool = True,
                  corrupt_pat_crc: bool = False) -> tuple[list[bytes], CCGen]:
    cc = CCGen()
    packets: list[bytes] = []
    pat = pat_section({PROGRAM_NUMBER: pmt_pid}, version=pat_version,
                      current_next=pat_current, corrupt_crc=corrupt_pat_crc)
    packets += packetize_section(PAT_PID, pat, cc)
    pmt = pmt_section(
        PROGRAM_NUMBER,
        [(VIDEO_TYPE, video_pid), (AUDIO_TYPE, audio_pid)],
        pcr_pid=pcr_pid, version=pmt_version, current_next=pmt_current,
    )
    packets += packetize_section(pmt_pid, pmt, cc)
    # Ensure at least 3 full packets so the bounded sync confirmation locks
    # even on the tiny table-only scenarios.
    null_cc = CCGen()
    for _ in range(max(0, 3 - len(packets))):
        packets.append(ts_packet(0x1FFF, b"\xFF" * 184, cc=null_cc.next(0x1FFF)))
    return packets, cc


def scenario_clean() -> Scenario:
    packets, cc = _base_program()
    video_payload = bytes((i * 7 + 3) & 0xFF for i in range(307))
    video_pes = pes_packet(0xE0, video_payload, pts=90_000 * 5)
    packets += packetize_pes(VIDEO_PID, video_pes, cc)
    audio_payload = bytes((i * 13 + 1) & 0xFF for i in range(150))
    audio_pes = pes_packet(0xC0, audio_payload, pts=90_000 * 5 - 3600)
    packets += packetize_pes(AUDIO_PID, audio_pes, cc)
    # PAT retransmission to exercise repeat handling.
    packets += packetize_section(PAT_PID,
                                 pat_section({PROGRAM_NUMBER: PMT_PID}), cc)
    # two null packets continuing the null-PID counter from _base_program
    packets += [ts_packet(0x1FFF, b"\xFF" * 184, cc=i) for i in (1, 2)]

    return Scenario(
        name="clean",
        data=b"".join(packets),
        expectation={
            "programs": {PROGRAM_NUMBER: PMT_PID},
            "streams": {VIDEO_PID: VIDEO_TYPE, AUDIO_PID: AUDIO_TYPE},
            "pat_version": 0,
            "pmt_version": 0,
            "pmt_pid": PMT_PID,
            "pcr_pid": VIDEO_PID,
            "pes": [
                {"pid": VIDEO_PID, "payload_bytes": 307, "complete": True,
                 "pts": 90_000 * 5, "gap": False},
                {"pid": AUDIO_PID, "payload_bytes": 150, "complete": True,
                 "pts": 90_000 * 5 - 3600, "gap": False},
            ],
            "duplicates": {},
            "lost": {},
        },
    )


def scenario_sync_loss() -> Scenario:
    clean = scenario_clean()
    packets = [clean.data[i:i + PACKET_SIZE]
               for i in range(0, len(clean.data), PACKET_SIZE)]
    # 55 garbage bytes at the front, 301 garbage bytes mid-stream, 7 trailing.
    prefix = bytes((i * 31 + 5) & 0x7F for i in range(55))
    middle = bytes((i * 17 + 9) & 0x7F for i in range(301))
    split = 4
    stream = prefix + b"".join(packets[:split]) + middle + b"".join(packets[split:])
    stream += b"\x12\x34\x56\x78\x9A\xBC\xDE"
    return Scenario(
        name="sync_loss",
        data=stream,
        expectation={
            "clean_packet_count": len(packets),
            "garbage_prefix": 55,
            "garbage_middle": 301,
            "trailing": 7,
            "programs": {PROGRAM_NUMBER: PMT_PID},
            "pes_count": 2,
        },
    )


def scenario_duplicate_packet() -> Scenario:
    packets, cc = _base_program()
    video_pes = pes_packet(0xE0, bytes(range(200)), pts=90_000)
    video_packets = packetize_pes(VIDEO_PID, video_pes, cc)
    # Duplicate the *second* video packet byte-for-byte (same CC, same data).
    video_packets.insert(2, video_packets[1])
    packets += video_packets
    return Scenario(
        name="duplicate_packet",
        data=b"".join(packets),
        expectation={
            "duplicates": {VIDEO_PID: 1},
            "lost": {},
            "pes_complete": True,  # duplicate payload is not appended twice
        },
    )


def scenario_missing_packets() -> Scenario:
    packets, cc = _base_program()
    # Payload spans 4 full TS packets + a short final packet, so dropping
    # interior packets leaves a recognisable CC gap instead of just a short
    # (legal) final packet.
    video_pes = pes_packet(0xE0, bytes((i * 5) & 0xFF for i in range(700)),
                           pts=180_000)
    video_packets = packetize_pes(VIDEO_PID, video_pes, cc)
    # 4 packets for this PES (14-byte header + 700 ES = 714 bytes). Drop the
    # two interior full packets at indices 1 and 2; the short final packet
    # remains and its CC (3) sits two steps past packet 0's CC (0), so the
    # analyzer reports exactly 2 missing and the PES ends both short and gapped.
    assert len(video_packets) == 4
    assert video_packets[3][3] & 0x0F == 3  # counters 0,1,2,3 across the PES
    del video_packets[1:3]
    packets += video_packets
    return Scenario(
        name="missing_packets",
        data=b"".join(packets),
        expectation={
            "lost": {VIDEO_PID: 2},
            "pes_gap": True,
            "pes_complete": False,
        },
    )


def scenario_adaptation_fields() -> Scenario:
    packets, cc = _base_program()
    # One payload packet, then:
    #  - legal adaptation-only packet reusing the CC (no increment, afc=2)
    #  - tolerated adaptation-only packet incrementing the CC (afc=2)
    #  - payload packet carrying PCR
    #  - discontinuity_indicator packet with an arbitrary CC jump
    p1 = ts_packet(VIDEO_PID, b"\xAA" * 40, cc=cc.next(VIDEO_PID), pusi=True,
                   force_afc=3)
    legal_af = ts_packet(VIDEO_PID, b"", cc=cc.peek(VIDEO_PID), force_afc=2)
    tolerated_cc = (cc.peek(VIDEO_PID) + 1) & 0xF
    tolerated = ts_packet(VIDEO_PID, b"", cc=tolerated_cc, force_afc=2)
    cc.set(VIDEO_PID, tolerated_cc)
    pcr_packet = ts_packet(
        VIDEO_PID, b"\xBB" * 40, cc=cc.next(VIDEO_PID),
        pcr=(27_000_000, 0), force_afc=3,
    )
    # declared discontinuity: jump CC from current to +4 without loss accounting
    jumped = (cc.peek(VIDEO_PID) + 4) & 0xF
    disc = ts_packet(
        VIDEO_PID, b"\xCC" * 40, cc=jumped, discontinuity=True, force_afc=3
    )
    cc.set(VIDEO_PID, jumped)
    after = ts_packet(VIDEO_PID, b"\xDD" * 40, cc=cc.next(VIDEO_PID),
                      force_afc=3)
    packets += [p1, legal_af, tolerated, pcr_packet, disc, after]
    return Scenario(
        name="adaptation_fields",
        data=b"".join(packets),
        expectation={
            "codes_present": [
                "af_only_cc_increment",
                "discontinuity_indicator",
            ],
            "codes_absent": ["continuity_lost", "duplicate_packet"],
            "declared_discontinuities": {VIDEO_PID: 1},
            "pcr_pid": VIDEO_PID,
        },
    )


def scenario_cross_packet_sections() -> Scenario:
    cc = CCGen()
    packets: list[bytes] = []
    pat = pat_section({PROGRAM_NUMBER: PMT_PID})
    packets += packetize_section(PAT_PID, pat, cc)
    # A PMT with 40 streams is large enough (~242 section bytes) to span two
    # packets; cutting after only 60 section bytes forces a genuine
    # cross-packet continuation while the start packet stays a full 184
    # payload. The test still asserts the same two declared elementary PIDs
    # (other PIDs are unknown to the analyzer and only get CC/stat treatment).
    cross_streams = [(VIDEO_TYPE, VIDEO_PID), (AUDIO_TYPE, AUDIO_PID)]
    cross_streams += [(0x06, 0x300 + i) for i in range(38)]
    pmt = pmt_section(PROGRAM_NUMBER, cross_streams, pcr_pid=VIDEO_PID)
    packets += packetize_raw_psi(PMT_PID, pmt, cc, first_section_bytes=60)
    video_pes = pes_packet(0xE0, bytes(range(250)), pts=45_000)
    packets += packetize_pes(VIDEO_PID, video_pes, cc)
    return Scenario(
        name="cross_packet_sections",
        data=b"".join(packets),
        expectation={
            "programs": {PROGRAM_NUMBER: PMT_PID},
            "streams": {VIDEO_PID: VIDEO_TYPE, AUDIO_PID: AUDIO_TYPE},
            "crc_errors": 0,
            "section_incomplete": 0,
        },
    )


def scenario_crc_error() -> Scenario:
    packets, _cc = _base_program(corrupt_pat_crc=True)
    return Scenario(
        name="crc_error",
        data=b"".join(packets),
        expectation={
            "crc_errors": 1,
            "programs": {},       # bad PAT never applied
            "verdict": "rejected",
        },
    )


def scenario_version_switch() -> Scenario:
    cc = CCGen()
    packets: list[bytes] = []
    # v0: PMT 0x100 with video 0x101 / audio 0x102
    packets += packetize_section(
        PAT_PID, pat_section({PROGRAM_NUMBER: PMT_PID}, version=0), cc
    )
    packets += packetize_section(
        PMT_PID,
        pmt_section(PROGRAM_NUMBER,
                    [(VIDEO_TYPE, VIDEO_PID), (AUDIO_TYPE, AUDIO_PID)],
                    version=0),
        cc,
    )
    # v1: PMT moves to 0x110; video moves to 0x121, audio dropped, new audio 0x122
    packets += packetize_section(
        PAT_PID, pat_section({PROGRAM_NUMBER: PMT_PID_V2}, version=1), cc
    )
    packets += packetize_section(
        PMT_PID_V2,
        pmt_section(PROGRAM_NUMBER,
                    [(VIDEO_TYPE, 0x0121), (AUDIO_TYPE, 0x0122)],
                    pcr_pid=0x0121, version=1),
        cc,
    )
    # unchanged retransmission of v1
    packets += packetize_section(
        PAT_PID, pat_section({PROGRAM_NUMBER: PMT_PID_V2}, version=1), cc
    )
    return Scenario(
        name="version_switch",
        data=b"".join(packets),
        expectation={
            "final_program": (PROGRAM_NUMBER, PMT_PID_V2),
            "final_streams": {0x0121: VIDEO_TYPE, 0x0122: AUDIO_TYPE},
            "final_pcr_pid": 0x0121,
            "pat_version_switches": 2,   # initial + v1 (repeat does not count)
            "pmt_version_switches": 2,
            "retired_pmt_pid": PMT_PID,
        },
    )


def scenario_not_current() -> Scenario:
    packets, _cc = _base_program(pat_current=False, pmt_current=False)
    return Scenario(
        name="not_current",
        data=b"".join(packets),
        expectation={
            "programs": {},
            # The not-current PAT is parsed on PID 0; the not-current PMT is
            # never routed because no current PAT advertises its PID.
            "table_not_current": 1,
        },
    )


def scenario_pcr_timing() -> Scenario:
    cc = CCGen()
    packets: list[bytes] = []
    packets += packetize_section(
        PAT_PID, pat_section({PROGRAM_NUMBER: PMT_PID}), cc
    )
    packets += packetize_section(
        PMT_PID,
        pmt_section(PROGRAM_NUMBER, [(VIDEO_TYPE, VIDEO_PID)], pcr_pid=VIDEO_PID),
        cc,
    )
    # 11 packets on video PID, PCR every packet at a nominal 10 ms cadence.
    # PCR base runs at 90 kHz: 10 ms == 900 base ticks (extension kept 0).
    for i in range(11):
        pcr_base = 900 * i
        packets.append(
            ts_packet(VIDEO_PID, bytes([i]) * 40, cc=cc.next(VIDEO_PID),
                      pusi=(i == 0), pcr=(pcr_base, 0))
        )
    return Scenario(
        name="pcr_timing",
        data=b"".join(packets),
        expectation={
            "pcr_pid": VIDEO_PID,
            "pcr_samples": 11,
            "mean_interval_ms": 10.0,
        },
    )


SCENARIOS = {
    "clean": scenario_clean,
    "sync_loss": scenario_sync_loss,
    "duplicate_packet": scenario_duplicate_packet,
    "missing_packets": scenario_missing_packets,
    "adaptation_fields": scenario_adaptation_fields,
    "cross_packet_sections": scenario_cross_packet_sections,
    "crc_error": scenario_crc_error,
    "version_switch": scenario_version_switch,
    "not_current": scenario_not_current,
    "pcr_timing": scenario_pcr_timing,
}
