"""Constrained PES reassembly.

"Constrained" means:

* Only PIDs declared as elementary streams by an active PMT are reassembled.
* Each reassembled PES is capped (``max_pes_bytes``); an over-long PES is
  rejected instead of buffered without bound.
* Packet loss (an unsignaled CC gap) invalidates the PES in flight; a
  *signaled* discontinuity resets assembly without declaring the bytes bad
  (the sender warned us).  Duplicate payload packets are skipped so a
  retransmitted fragment is not concatenated twice.
* A PES with a known ``packet_length`` closes automatically as soon as its
  declared bytes have arrived -- no need to wait for the next PUSI and no
  risk of swallowing the next PES or trailing 0xFF TS stuffing.  Unbounded
  PES (video with packet_length==0) closes at the next PUSI instead.
* PES start-code / header shape is validated; PTS/DTS are decoded only when
  present and marker bits are checked.
"""
from __future__ import annotations

from dataclasses import dataclass

from .diagnostics import (
    PES_GAP,
    PES_INCOMPLETE,
    PES_MALFORMED,
    PES_OVERSIZE,
    Disposition,
    Finding,
    Severity,
)

PES_START = b"\x00\x00\x01"
_OPTIONAL_HEADER_IDS = set(range(0xC0, 0xF0))  # audio 0xC0-0xDF, video 0xE0-0xEF
_PROGRAM_STREAM_IDS = {0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xFF, 0xF2, 0xF8}


@dataclass
class PesHeader:
    stream_id: int
    pes_length: int          # 0 means unbounded (video)
    pts: int | None = None
    dts: int | None = None
    header_size: int = 0


@dataclass
class PesFragment:
    pid: int
    start_packet_index: int
    end_packet_index: int | None
    header: PesHeader | None
    data: bytes = b""        # reassembled PES packet bytes (header + payload)
    corrupt: bool = False
    corrupt_reason: str | None = None
    corrupt_code: str = PES_GAP
    gap_packets: int = 0


@dataclass
class _PesTiming:
    pid: int
    packet_index: int
    pts: int | None
    dts: int | None


def parse_pes_header(data: bytes) -> tuple[PesHeader | None, str | None]:
    """Parse a PES header from the start of a reassembled PES.

    Returns (header, None) on success or (None, reason) when rejected.
    """
    if len(data) < 6 or data[:3] != PES_START:
        return None, "missing PES start code prefix"
    stream_id = data[3]
    pes_length = int.from_bytes(data[4:6], "big")

    if stream_id in _PROGRAM_STREAM_IDS:
        return PesHeader(stream_id=stream_id, pes_length=pes_length,
                         header_size=6), None
    if stream_id not in _OPTIONAL_HEADER_IDS:
        return None, f"unsupported stream_id 0x{stream_id:02x}"
    if len(data) < 9:
        return None, "truncated PES optional header"

    pts_dts_flags = (data[7] >> 6) & 0x03
    header_data_len = data[8]
    header_size = 9 + header_data_len
    if len(data) < header_size:
        return None, "PES header_data_length overruns available bytes"

    pts = dts = None

    def _read_ts(off: int) -> int:
        return (
            ((data[off] >> 1) & 0x07) << 30
            | data[off + 1] << 22
            | ((data[off + 2] >> 1) & 0x7F) << 15
            | data[off + 3] << 7
            | ((data[off + 4] >> 1) & 0x7F)
        )

    if pts_dts_flags == 0b10:
        if header_data_len < 5 or (data[9] & 0xF0) != 0x20:
            return None, "malformed PTS field"
        pts = _read_ts(9)
    elif pts_dts_flags == 0b11:
        if (header_data_len < 10
                or (data[9] & 0xF0) != 0x30
                or (data[14] & 0xF0) != 0x10):
            return None, "malformed PTS/DTS fields"
        pts = _read_ts(9)
        dts = _read_ts(14)
    elif pts_dts_flags == 0b00:
        pass
    else:
        return None, f"reserved PTS_DTS_flags {pts_dts_flags}"

    return PesHeader(stream_id=stream_id, pes_length=pes_length, pts=pts,
                     dts=dts, header_size=header_size), None


class PesAssembler:
    """Per-PID reassembly state."""

    def __init__(self, pid: int, stream_type: int, program_number: int,
                 max_pes_bytes: int) -> None:
        self.pid = pid
        self.stream_type = stream_type
        self.program_number = program_number
        self.max_pes_bytes = max_pes_bytes
        self._frag: PesFragment | None = None
        self._declared_total: int | None = None  # 6 + packet_length
        self.completed: list[PesFragment] = []
        self.findings: list[Finding] = []
        self.timing: list[_PesTiming] = []
        self.gap_count = 0

    def _finding(self, code: str, severity: Severity,
                 disposition: Disposition, message: str,
                 packet_index: int, **details: object) -> Finding:
        return Finding(code=code, severity=severity, disposition=disposition,
                       message=message, packet_index=packet_index,
                       pid=self.pid, details=details)

    # ------------------------------------------------------------------
    def _start(self, payload: bytes, packet_index: int) -> None:
        self._frag = PesFragment(
            pid=self.pid, start_packet_index=packet_index,
            end_packet_index=None, header=None)
        self._declared_total = None
        if len(payload) >= 6 and payload[:3] == PES_START:
            plen = int.from_bytes(payload[4:6], "big")
            self._declared_total = 6 + plen if plen > 0 else None
        self._append(payload, packet_index)

    def _append(self, payload: bytes, packet_index: int) -> None:
        frag = self._frag
        assert frag is not None
        if frag.corrupt:
            # Still consume bytes so the fragment reaches its declared end,
            # but it will be reported corrupt when it closes.
            if self._declared_total is not None:
                room = self._declared_total - len(frag.data)
                if room > 0:
                    frag.data += payload[:room]
                if len(frag.data) >= self._declared_total:
                    self._close(packet_index)
            return
        if self._declared_total is not None:
            room = self._declared_total - len(frag.data)
            if room < 0:
                room = 0
            payload = payload[:room]
        elif len(frag.data) + len(payload) > self.max_pes_bytes:
            frag.corrupt = True
            frag.corrupt_reason = (
                f"reassembly exceeded {self.max_pes_bytes} bytes")
            frag.corrupt_code = PES_OVERSIZE
            self.findings.append(self._finding(
                PES_OVERSIZE, Severity.ERROR, Disposition.REJECTED,
                "PES fragment exceeds reassembly cap",
                packet_index, bytes_buffered=len(frag.data),
                incoming_bytes=len(payload),
                cap_bytes=self.max_pes_bytes))
            self._close(packet_index)
            return
        frag.data += payload
        if (self._declared_total is not None
                and len(frag.data) >= self._declared_total):
            self._close(packet_index)

    def _close(self, packet_index: int | None) -> None:
        frag = self._frag
        if frag is None:
            return
        frag.end_packet_index = packet_index
        if not frag.corrupt:
            header, why = parse_pes_header(frag.data)
            if header is None:
                frag.corrupt = True
                frag.corrupt_reason = why
                frag.corrupt_code = PES_MALFORMED
                self.findings.append(self._finding(
                    PES_MALFORMED, Severity.ERROR, Disposition.REJECTED,
                    f"PES header rejected: {why}",
                    packet_index if packet_index is not None else -1,
                    bytes_buffered=len(frag.data)))
            else:
                frag.header = header
                if header.pts is not None or header.dts is not None:
                    self.timing.append(_PesTiming(
                        self.pid, frag.start_packet_index,
                        header.pts, header.dts))
        self.completed.append(frag)
        if frag.corrupt:
            self.gap_count += 1
            self.findings.append(self._finding(
                frag.corrupt_code, Severity.ERROR, Disposition.REJECTED,
                f"PES fragment discarded: {frag.corrupt_reason}",
                packet_index if packet_index is not None else -1,
                bytes_buffered=len(frag.data),
                missing_packets=frag.gap_packets))
        self._frag = None
        self._declared_total = None

    # ------------------------------------------------------------------
    def signal_loss(self, packet_index: int, missing: int,
                    signaled: bool) -> None:
        if self._frag is None:
            return
        if signaled:
            # Signaled discontinuity: discard in-flight fragment without
            # counting a gap; assembly resumes at the next PUSI.
            self._frag = None
            self._declared_total = None
            return
        # Mark corrupt but keep buffering until the fragment ends (self-close
        # by length or the next PUSI); the gap is reported at that point.
        self._frag.corrupt = True
        self._frag.corrupt_reason = (
            f"{missing} packet(s) lost within PES (unsignaled CC gap)")
        self._frag.corrupt_code = PES_GAP
        self._frag.gap_packets += missing

    def signal_tei(self, packet_index: int) -> None:
        if self._frag is None:
            return
        self._frag.corrupt = True
        self._frag.corrupt_reason = (
            "packet with transport_error_indicator inside PES")
        self._frag.corrupt_code = "pes_transport_error"

    def reset(self, packet_index: int) -> None:
        """Discard in-flight assembly after a sync-loss resync."""
        if self._frag is not None:
            self._frag = None
            self._declared_total = None

    def feed(self, payload: bytes, pusi: bool, duplicate: bool,
             packet_index: int) -> None:
        if duplicate:
            return  # exact retransmit: never append twice
        if pusi:
            # Close the previous fragment if it is still open. A length
            # bounded healthy PES self-closed already; anything left here is
            # unbounded (valid), truncated, or corrupt from a CC gap.
            if self._frag is not None:
                frag = self._frag
                if not frag.corrupt and self._declared_total is not None:
                    frag.corrupt = True
                    frag.corrupt_reason = (
                        "new PUSI before the bounded PES reached its declared "
                        "length (missing bytes)")
                    frag.corrupt_code = PES_GAP
                self._close(packet_index)
            self._start(payload, packet_index)
            return
        if self._frag is None:
            self.findings.append(self._finding(
                PES_MALFORMED, Severity.WARNING, Disposition.UNDETERMINED,
                "continuation payload with no PES start observed",
                packet_index, payload_bytes=len(payload)))
            return
        self._append(payload, packet_index)

    def flush_end(self) -> None:
        if self._frag is not None:
            frag = self._frag
            if not frag.corrupt and self._declared_total is not None:
                frag.corrupt = True
                frag.corrupt_reason = (
                    f"only {len(frag.data)} of {self._declared_total} "
                    "declared bytes arrived")
                frag.corrupt_code = PES_GAP
            # Unbounded PES (packet_length==0) ending at EOF is acceptable.
            self._close(frag.start_packet_index)
