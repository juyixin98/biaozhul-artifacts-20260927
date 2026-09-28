"""MPEG-TS packet header and adaptation field parsing (188-byte packets).

This module is pure parsing: it holds no per-PID state and knows nothing
about PSI/PES semantics. All offsets in the parsed objects are absolute
byte offsets inside the byte stream handed to the analyzer, which makes
diagnostics point at a concrete record location.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

TS_PACKET_SIZE = 188
SYNC_BYTE = 0x47

NULL_PID = 0x1FFF
PAT_PID = 0x0000

# adaptation_field_control values
AFC_RESERVED = 0
AFC_PAYLOAD_ONLY = 1
AFC_ADAPTATION_ONLY = 2
AFC_BOTH = 3


class PacketParseError(ValueError):
    """A packet whose declared structure overruns the 188-byte packet."""


@dataclass(frozen=True)
class AdaptationField:
    length: int
    discontinuity_indicator: bool
    random_access_indicator: bool
    es_priority_indicator: bool
    pcr_flag: bool
    opcr_flag: bool
    splicing_point_flag: bool
    transport_private_data_flag: bool
    adaptation_field_extension_flag: bool
    pcr: Optional[int]  # 48-bit PCR as base*300 + extension, or None
    opcr: Optional[int]
    splice_countdown: Optional[int]

    @property
    def declared_discontinuity(self) -> bool:
        return self.discontinuity_indicator


@dataclass(frozen=True)
class PacketHeader:
    offset: int
    tei: bool                      # transport_error_indicator
    pusi: bool                     # payload_unit_start_indicator
    transport_priority: bool
    pid: int
    scrambling_control: int
    adaptation_field_control: int
    continuity_counter: int
    adaptation_field: Optional[AdaptationField]
    payload_offset: int            # absolute offset; == offset+188 when no payload
    payload_length: int

    @property
    def has_adaptation_field(self) -> bool:
        return self.adaptation_field_control in (AFC_ADAPTATION_ONLY, AFC_BOTH)

    @property
    def has_payload(self) -> bool:
        return self.adaptation_field_control in (AFC_PAYLOAD_ONLY, AFC_BOTH)

    @property
    def is_scrambled(self) -> bool:
        return self.scrambling_control != 0


def parse_adaptation_field(buf: bytes) -> AdaptationField:
    """Parse the adaptation field body (the bytes following the length byte)."""
    if not buf:
        # length == 0 is legal for a single stuffing byte of value 0.
        return AdaptationField(
            length=0,
            discontinuity_indicator=False,
            random_access_indicator=False,
            es_priority_indicator=False,
            pcr_flag=False,
            opcr_flag=False,
            splicing_point_flag=False,
            transport_private_data_flag=False,
            adaptation_field_extension_flag=False,
            pcr=None,
            opcr=None,
            splice_countdown=None,
        )
    flags = buf[0]
    pcr_flag = bool(flags & 0x10)
    opcr_flag = bool(flags & 0x08)
    splicing_flag = bool(flags & 0x04)
    private_flag = bool(flags & 0x02)
    extension_flag = bool(flags & 0x01)
    pos = 1

    def read_pcr() -> Optional[int]:
        nonlocal pos
        if pos + 6 > len(buf):
            return None
        b0, b1, b2, b3, b4, b5 = buf[pos:pos + 6]
        pos += 6
        pcr_base = (b0 << 25) | (b1 << 17) | (b2 << 9) | (b3 << 1) | (b4 >> 7)
        pcr_ext = ((b4 & 0x01) << 8) | b5
        return pcr_base * 300 + pcr_ext

    pcr = read_pcr() if pcr_flag else None
    opcr = read_pcr() if opcr_flag else None
    splice_countdown: Optional[int] = None
    if splicing_flag and pos < len(buf):
        # int8
        value = buf[pos]
        splice_countdown = value - 256 if value >= 128 else value
        pos += 1
    # private data / extension lengths are not needed downstream; skip parsing
    return AdaptationField(
        length=len(buf),
        discontinuity_indicator=bool(flags & 0x80),
        random_access_indicator=bool(flags & 0x40),
        es_priority_indicator=bool(flags & 0x20),
        pcr_flag=pcr_flag,
        opcr_flag=opcr_flag,
        splicing_point_flag=splicing_flag,
        transport_private_data_flag=private_flag,
        adaptation_field_extension_flag=extension_flag,
        pcr=pcr,
        opcr=opcr,
        splice_countdown=splice_countdown,
    )


def parse_packet_header(data: bytes | bytearray | memoryview, offset: int = 0) -> PacketHeader:
    """Parse one 188-byte packet starting at ``offset``.

    Raises PacketParseError if the sync byte is wrong or the declared
    adaptation field overruns the packet boundary.
    """
    if offset < 0 or offset + TS_PACKET_SIZE > len(data):
        raise PacketParseError(f"packet at offset {offset} does not fit in input")
    if data[offset] != SYNC_BYTE:
        raise PacketParseError(f"expected sync byte 0x47 at offset {offset}")

    b1 = data[offset + 1]
    b2 = data[offset + 2]
    b3 = data[offset + 3]

    tei = bool(b1 & 0x80)
    pusi = bool(b1 & 0x40)
    priority = bool(b1 & 0x20)
    pid = ((b1 & 0x1F) << 8) | b2
    scrambling = (b3 >> 6) & 0x03
    afc = (b3 >> 4) & 0x03
    cc = b3 & 0x0F

    if afc == AFC_RESERVED:
        raise PacketParseError(f"reserved adaptation_field_control at offset {offset}")

    pos = offset + 4
    adaptation: Optional[AdaptationField] = None
    if afc in (AFC_ADAPTATION_ONLY, AFC_BOTH):
        af_length = data[pos]
        af_start = pos + 1
        af_end = af_start + af_length
        if af_end > offset + TS_PACKET_SIZE:
            raise PacketParseError(
                f"adaptation field length {af_length} overruns packet at offset {offset}"
            )
        adaptation = parse_adaptation_field(bytes(data[af_start:af_end]))
        pos = af_end

    payload_offset = pos if afc in (AFC_PAYLOAD_ONLY, AFC_BOTH) else offset + TS_PACKET_SIZE
    payload_length = max(0, offset + TS_PACKET_SIZE - payload_offset)

    return PacketHeader(
        offset=offset,
        tei=tei,
        pusi=pusi,
        transport_priority=priority,
        pid=pid,
        scrambling_control=scrambling,
        adaptation_field_control=afc,
        continuity_counter=cc,
        adaptation_field=adaptation,
        payload_offset=payload_offset,
        payload_length=payload_length,
    )
