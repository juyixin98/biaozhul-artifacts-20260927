"""PSI section reassembly and PAT/PMT section parsing.

Sections arrive as PUSI-terminated units spread across TS packets, with a
pointer_field on the start packet and 0xFF stuffing after section ends.
Reassembly is per-PID, bounds-checked, and verified with the MPEG-2
CRC before a section is handed to the atomic table store.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..diagnostics import Code, DiagnosticsCollector
from .crc32 import mpeg_crc32

# table_id values
TABLE_ID_PAT = 0x00
TABLE_ID_PMT = 0x02


@dataclass(frozen=True)
class Section:
    raw: bytes
    table_id: int
    section_syntax_indicator: bool
    section_length: int
    table_id_extension: int
    version: int
    current_next: bool
    section_number: int
    last_section_number: int
    body: bytes              # bytes between the 8-byte header and the CRC
    stored_crc: int
    computed_crc: int

    @property
    def crc_ok(self) -> bool:
        return self.stored_crc == self.computed_crc


@dataclass(frozen=True)
class PATProgram:
    program_number: int
    pid: int                # PMT PID for a program; network PID for program 0


@dataclass(frozen=True)
class PMTStream:
    stream_type: int
    pid: int


@dataclass(frozen=True)
class ParsedPAT:
    transport_stream_id: int
    version: int
    current_next: bool
    programs: tuple[PATProgram, ...]


@dataclass(frozen=True)
class ParsedPMT:
    program_number: int
    version: int
    current_next: bool
    pcr_pid: Optional[int]
    streams: tuple[PMTStream, ...]


def parse_section(raw: bytes) -> Optional[Section]:
    """Parse one complete section (header .. CRC). None if structurally bad."""
    if len(raw) < 8:
        return None
    table_id = raw[0]
    section_syntax = bool(raw[1] & 0x80)
    section_length = ((raw[1] & 0x0F) << 8) | raw[2]
    if 3 + section_length != len(raw) or len(raw) < 3 + section_length:
        return None
    table_id_extension = (raw[3] << 8) | raw[4]
    version = (raw[5] >> 1) & 0x1F
    current_next = bool(raw[5] & 0x01)
    section_number = raw[6]
    last_section_number = raw[7]
    if section_length < 4 + 5:
        return None
    body = bytes(raw[8:-4])
    stored_crc = int.from_bytes(raw[-4:], "big")
    computed_crc = mpeg_crc32(raw[:-4])
    return Section(
        raw=bytes(raw),
        table_id=table_id,
        section_syntax_indicator=section_syntax,
        section_length=section_length,
        table_id_extension=table_id_extension,
        version=version,
        current_next=current_next,
        section_number=section_number,
        last_section_number=last_section_number,
        body=body,
        stored_crc=stored_crc,
        computed_crc=computed_crc,
    )


def parse_pat(section: Section) -> ParsedPAT:
    programs: list[PATProgram] = []
    body = section.body
    for i in range(0, len(body) - 3, 4):
        program_number = (body[i] << 8) | body[i + 1]
        pid = ((body[i + 2] & 0x1F) << 8) | body[i + 3]
        programs.append(PATProgram(program_number=program_number, pid=pid))
    return ParsedPAT(
        transport_stream_id=section.table_id_extension,
        version=section.version,
        current_next=section.current_next,
        programs=tuple(programs),
    )


def parse_pmt(section: Section) -> ParsedPMT:
    body = section.body
    if len(body) < 4:
        return ParsedPMT(
            program_number=section.table_id_extension,
            version=section.version,
            current_next=section.current_next,
            pcr_pid=None,
            streams=(),
        )
    pcr_pid_raw = ((body[0] & 0x1F) << 8) | body[1]
    program_info_length = ((body[2] & 0x0F) << 8) | body[3]
    pcr_pid: Optional[int] = pcr_pid_raw if pcr_pid_raw != 0x1FFF else None
    streams: list[PMTStream] = []
    pos = 4 + program_info_length
    while pos + 5 <= len(body):
        stream_type = body[pos]
        pid = ((body[pos + 1] & 0x1F) << 8) | body[pos + 2]
        es_info_length = ((body[pos + 3] & 0x0F) << 8) | body[pos + 4]
        streams.append(PMTStream(stream_type=stream_type, pid=pid))
        pos += 5 + es_info_length
    return ParsedPMT(
        program_number=section.table_id_extension,
        version=section.version,
        current_next=section.current_next,
        pcr_pid=pcr_pid,
        streams=tuple(streams),
    )


class SectionAssembler:
    """Reassembles sections for a single PID from packet payloads."""

    def __init__(
        self,
        pid: int,
        diagnostics: DiagnosticsCollector,
        max_section_bytes: int,
    ):
        self._pid = pid
        self._diag = diagnostics
        self._max_section_bytes = max_section_bytes
        self._buf = bytearray()
        self._expected: Optional[int] = None

    def reset_on_gap(self, offset: Optional[int]) -> None:
        """Discard a half-assembled section after a known CC loss."""
        if self._buf:
            self._diag.warning(
                Code.SECTION_GAP,
                "partial section discarded due to continuity loss on its PID",
                pid=self._pid,
                offset=offset,
                buffered_bytes=len(self._buf),
            )
        self._buf.clear()
        self._expected = None

    def feed(
        self, pusi: bool, payload: bytes, packet_offset: int
    ) -> list[Section]:
        out: list[Section] = []
        if pusi:
            if not payload:
                if self._buf:
                    self._diag.warning(
                        Code.SECTION_INCOMPLETE,
                        "new section started before previous section completed",
                        pid=self._pid,
                        offset=packet_offset,
                        buffered_bytes=len(self._buf),
                    )
                self._buf.clear()
                self._expected = None
                return out
            pointer_field = payload[0]
            usable_start = 1 + pointer_field
            if usable_start > len(payload):
                self._diag.error(
                    Code.POINTER_OUT_OF_RANGE,
                    "pointer_field points past packet payload",
                    pid=self._pid,
                    offset=packet_offset,
                    pointer_field=pointer_field,
                    payload_length=len(payload),
                )
                self._buf.clear()
                self._expected = None
                return out
            if pointer_field:
                # Bytes before the new section start are the tail of the
                # previous section: complete it first, then reset.
                self._buf.extend(payload[1:usable_start])
                out.extend(self._drain(packet_offset))
            if self._buf:
                self._diag.warning(
                    Code.SECTION_INCOMPLETE,
                    "new section started before previous section completed",
                    pid=self._pid,
                    offset=packet_offset,
                    buffered_bytes=len(self._buf),
                )
            self._buf.clear()
            self._expected = None
            payload = payload[usable_start:]
        self._buf.extend(payload)
        out.extend(self._drain(packet_offset))
        return out

    def _drain(self, packet_offset: int) -> list[Section]:
        out: list[Section] = []
        while True:
            if self._expected is None:
                while self._buf and self._buf[0] == 0xFF:
                    del self._buf[0]
                if len(self._buf) < 3:
                    break
                section_length = ((self._buf[1] & 0x0F) << 8) | self._buf[2]
                total = 3 + section_length
                if total > self._max_section_bytes:
                    self._diag.error(
                        Code.SECTION_OVERSIZED,
                        "section_length exceeds configured maximum",
                        pid=self._pid,
                        offset=packet_offset,
                        section_length=section_length,
                        max_section_bytes=self._max_section_bytes,
                    )
                    self._buf.clear()
                    break
                self._expected = total
            if len(self._buf) < self._expected:
                break
            raw = bytes(self._buf[: self._expected])
            del self._buf[: self._expected]
            self._expected = None
            section = parse_section(raw)
            if section is not None:
                out.append(section)
        return out

    def flush(self, end_offset: int) -> None:
        if self._buf:
            self._diag.warning(
                Code.SECTION_INCOMPLETE,
                "section truncated at end of input",
                pid=self._pid,
                offset=end_offset,
                buffered_bytes=len(self._buf),
            )
            self._buf.clear()
            self._expected = None
