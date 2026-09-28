"""Restricted PES reassembly.

Scope ("受限"):

* Only PIDs declared as elementary streams in a current PMT are fed here.
* PES start codes (0x000001), lengths, PTS/DTS and payload boundaries are
  parsed; the encoded payload itself is never decoded.
* One PES packet per PID is buffered at a time, bounded by
  ``max_pes_payload_bytes``. Overflow is reported and the excess is not
  buffered.
* Gaps are tracked explicitly: a continuity loss on the PID marks the
  open PES packet as containing a gap rather than silently joining bytes
  across the loss.
* ``PES_packet_length == 0`` (legal for video) yields an unbounded
  packet terminated by the next PUSI on the PID.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..diagnostics import Code, DiagnosticsCollector

START_CODE_PREFIX = b"\x00\x00\x01"

# Stream IDs whose PES packet has no optional PES header (ISO/IEC 13818-1 §2.4.3.6).
_NO_OPTIONAL_HEADER = frozenset(
    {
        0xBC,  # program_stream_map
        0xBE,  # padding_stream
        0xBF,  # private_stream_2
        0xF0,  # ECM
        0xF1,  # EMM
        0xF2,  # DSMCC stream
        0xF8,  # ITU-T H.222.0 type E
        0xFF,  # program_stream_directory
    }
)

PTS_DTS_PTS_ONLY = 0x2
PTS_DTS_PTS_AND_DTS = 0x3


@dataclass(frozen=True)
class PESHeader:
    stream_id: int
    pes_packet_length: int       # declared length, may be 0 (unbounded video)
    header_data_length: int
    pts: Optional[int]           # 90 kHz ticks
    dts: Optional[int]

    @property
    def declared_total_length(self) -> Optional[int]:
        """Total PES bytes (prefix..end) implied by the length field, or None."""
        if self.pes_packet_length == 0:
            return None
        return 6 + self.pes_packet_length


@dataclass
class PESRecord:
    pid: int
    start_offset: int
    end_offset: int
    packet_count: int = 0
    header: Optional[PESHeader] = None
    payload_bytes: int = 0
    complete: bool = False
    gap: bool = False
    capped: bool = False
    dropped: bool = False
    close_reason: Optional[str] = None  # "next_pusi" | "end_of_input"

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "packet_count": self.packet_count,
            "stream_id": None if self.header is None else self.header.stream_id,
            "pes_packet_length": (
                None if self.header is None else self.header.pes_packet_length
            ),
            "pts": None if self.header is None else self.header.pts,
            "dts": None if self.header is None else self.header.dts,
            "payload_bytes": self.payload_bytes,
            "complete": self.complete,
            "gap": self.gap,
            "capped": self.capped,
            "dropped": self.dropped,
            "close_reason": self.close_reason,
        }


@dataclass
class _OpenPES:
    start_offset: int
    end_offset: int
    packet_count: int = 0
    data: bytearray = field(default_factory=bytearray)
    header: Optional[PESHeader] = None
    header_done: bool = False
    bad_start: bool = False
    gap: bool = False
    capped: bool = False
    payload_start: int = 0
    saw_final_partial: bool = False
    next_es_offset: int = -1  # start offset of first ES byte after PES ends (-1: none yet)


def _read_timestamp(buf: bytes, pos: int) -> Optional[int]:
    if pos + 5 > len(buf):
        return None
    return (
        ((buf[pos] >> 1) & 0x07) << 30
        | buf[pos + 1] << 22
        | ((buf[pos + 2] >> 1) << 15)
        | buf[pos + 3] << 7
        | (buf[pos + 4] >> 1)
    )


def parse_pes_header(buf: bytes) -> Optional[PESHeader]:
    """Parse a PES header from the start of ``buf`` if enough bytes are present."""
    if len(buf) < 6 or bytes(buf[:3]) != START_CODE_PREFIX:
        return None
    stream_id = buf[3]
    pes_length = (buf[4] << 8) | buf[5]

    if stream_id in _NO_OPTIONAL_HEADER:
        return PESHeader(
            stream_id=stream_id,
            pes_packet_length=pes_length,
            header_data_length=0,
            pts=None,
            dts=None,
        )

    if len(buf) < 9:
        return None
    header_data_length = buf[8]
    if len(buf) < 9 + header_data_length:
        return None
    pts_dts_flags = (buf[7] >> 6) & 0x03
    pts: Optional[int] = None
    dts: Optional[int] = None
    if pts_dts_flags in (PTS_DTS_PTS_ONLY, PTS_DTS_PTS_AND_DTS):
        pts = _read_timestamp(buf, 9)
    if pts_dts_flags == PTS_DTS_PTS_AND_DTS:
        dts = _read_timestamp(buf, 14)
    return PESHeader(
        stream_id=stream_id,
        pes_packet_length=pes_length,
        header_data_length=header_data_length,
        pts=pts,
        dts=dts,
    )


class PesAssembler:
    def __init__(
        self,
        diagnostics: DiagnosticsCollector,
        max_payload_bytes: int,
    ):
        self._diag = diagnostics
        self._max_payload = max_payload_bytes
        self._open: dict[int, _OpenPES] = {}
        self._midstream_warned: set[int] = set()
        self.records: list[PESRecord] = []

    def mark_gap(self, pid: int, offset: Optional[int]) -> None:
        open_pes = self._open.get(pid)
        if open_pes is not None:
            open_pes.gap = True
            self._diag.warning(
                Code.PES_GAP,
                "PES packet marked with gap due to continuity loss",
                pid=pid,
                offset=offset,
                pes_start_offset=open_pes.start_offset,
            )

    def feed(
        self, pid: int, pusi: bool, payload: bytes, packet_offset: int
    ) -> list[PESRecord]:
        closed: list[PESRecord] = []
        if pusi:
            record = self._close(pid, reason="next_pusi")
            if record is not None:
                closed.append(record)
            self._open[pid] = _OpenPES(
                start_offset=packet_offset, end_offset=packet_offset
            )
            self._midstream_warned.discard(pid)

        open_pes = self._open.get(pid)
        if open_pes is None:
            if pid not in self._midstream_warned:
                self._diag.warning(
                    Code.PES_MIDSTREAM_DATA,
                    "elementary stream bytes without a PES start; skipped until next PUSI",
                    pid=pid,
                    offset=packet_offset,
                )
                self._midstream_warned.add(pid)
            return closed

        open_pes.end_offset = packet_offset
        open_pes.packet_count += 1

        if open_pes.bad_start:
            return closed

        header = open_pes.header

        # A short payload (<184) means this packet ended the TS payload area
        # (stuffing carried via adaptation field); used for length-0 PES.
        open_pes.saw_final_partial = len(payload) < 184

        accepted = payload
        if header is not None and header.pes_packet_length != 0:
            if header.stream_id in _NO_OPTIONAL_HEADER:
                es_total = header.pes_packet_length
            else:
                es_total = header.pes_packet_length - 3 - header.header_data_length
            es_so_far = max(0, len(open_pes.data) - open_pes.payload_start)
            remaining = max(0, es_total - es_so_far)
            accepted = payload[:remaining]
            if len(payload) > remaining:
                # Declared boundary reached mid-packet: rest is stuffing.
                open_pes.next_es_offset = packet_offset + len(accepted)

        room = self._max_payload - len(open_pes.data)
        if room > 0:
            chunk = accepted[:room]
            open_pes.data.extend(chunk)
            if len(chunk) < len(accepted):
                self._mark_capped(open_pes, pid, packet_offset)
        elif accepted:
            self._mark_capped(open_pes, pid, packet_offset)

        if not open_pes.header_done:
            if len(open_pes.data) >= 3 and bytes(open_pes.data[:3]) != START_CODE_PREFIX:
                open_pes.bad_start = True
                self._diag.error(
                    Code.PES_BAD_START_CODE,
                    "PUSI payload does not begin with 0x000001 start code",
                    pid=pid,
                    offset=packet_offset,
                    first_bytes=bytes(open_pes.data[:3]),
                )
                return closed
            parsed = parse_pes_header(open_pes.data)
            if parsed is not None:
                open_pes.header = parsed
                open_pes.header_done = True
                if parsed.stream_id in _NO_OPTIONAL_HEADER:
                    open_pes.payload_start = 6
                else:
                    open_pes.payload_start = 9 + parsed.header_data_length
                # The header arrived in the same packet as some ES bytes
                # (and possibly stuffing). Trim any trailing stuffing beyond
                # the declared PES length that was buffered before we knew it.
                if parsed.pes_packet_length != 0:
                    if parsed.stream_id in _NO_OPTIONAL_HEADER:
                        es_total = parsed.pes_packet_length
                    else:
                        es_total = (
                            parsed.pes_packet_length - 3 - parsed.header_data_length
                        )
                    allowed = open_pes.payload_start + es_total
                    if len(open_pes.data) > allowed:
                        del open_pes.data[allowed:]
        return closed

    def _mark_capped(self, open_pes: _OpenPES, pid: int, offset: int) -> None:
        if not open_pes.capped:
            open_pes.capped = True
            self._diag.warning(
                Code.PES_OVERFLOW_CAP,
                "PES payload exceeds buffering cap; remaining bytes not assembled",
                pid=pid,
                offset=offset,
                cap_bytes=self._max_payload,
                pes_start_offset=open_pes.start_offset,
            )

    def _close(self, pid: int, reason: str) -> Optional[PESRecord]:
        open_pes = self._open.pop(pid, None)
        if open_pes is None:
            return None
        record = self._build_record(open_pes, pid, reason)
        self.records.append(record)
        return record

    def _build_record(
        self, open_pes: _OpenPES, pid: int, reason: str
    ) -> PESRecord:
        header = open_pes.header
        if open_pes.bad_start:
            return PESRecord(
                pid=pid,
                start_offset=open_pes.start_offset,
                end_offset=open_pes.end_offset,
                packet_count=open_pes.packet_count,
                header=None,
                payload_bytes=0,
                complete=False,
                gap=open_pes.gap,
                capped=open_pes.capped,
                dropped=True,
                close_reason=reason,
            )

        payload_bytes = max(0, len(open_pes.data) - open_pes.payload_start)
        complete = False
        if header is not None:
            if header.pes_packet_length == 0:
                # Length 0 is legal for video elementary streams: the packet
                # is delimited by the next PUSI, or by a short final payload
                # packet at the very end of the input.
                delimited = reason == "next_pusi" or (
                    reason == "end_of_input" and open_pes.saw_final_partial
                )
                complete = delimited and not open_pes.capped
            else:
                if header.stream_id in _NO_OPTIONAL_HEADER:
                    es_total = header.pes_packet_length
                else:
                    es_total = header.pes_packet_length - 3 - header.header_data_length
                complete = payload_bytes >= es_total and not open_pes.capped
        return PESRecord(
            pid=pid,
            start_offset=open_pes.start_offset,
            end_offset=open_pes.end_offset,
            packet_count=open_pes.packet_count,
            header=header,
            payload_bytes=payload_bytes,
            complete=complete,
            gap=open_pes.gap,
            capped=open_pes.capped,
            dropped=False,
            close_reason=reason,
        )

    def flush(self, end_offset: int) -> list[PESRecord]:
        """Close everything still open at end of input as incomplete."""
        out: list[PESRecord] = []
        for pid in list(self._open.keys()):
            record = self._close(pid, reason="end_of_input")
            if record is not None:
                if not record.dropped and not record.complete:
                    self._diag.warning(
                        Code.PES_INCOMPLETE,
                        "PES packet not terminated before end of input",
                        pid=pid,
                        offset=end_offset,
                        pes_start_offset=record.start_offset,
                    )
                out.append(record)
        return out

    def gap_records(self) -> list[PESRecord]:
        return [r for r in self.records if r.gap and not r.dropped]
