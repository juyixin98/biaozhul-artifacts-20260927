"""PSI section reassembly and PAT/PMT interpretation.

Responsibilities split in two:

:class:`SectionAssembler`
    Per-PID byte stream reassembly of MPEG-2 private sections, honoring the
    ``pointer_field`` on payload-unit-start packets, stuffing bytes, and
    sections that span several TS packets.  Verifies section length, table-id
    shape and CRC32/MPEG-2 before handing a section out.

:class:`TablesManager`
    Feeds reassembled sections, parses PAT/PMT, and holds program maps.
    Version switches are applied **atomically**: a complete,
    CRC-verified newer version replaces the previous map in one operation;
    a bad-CRC or truncated new version never mutates the current map.

Nothing here assumes fixed PIDs beyond the PAT (0x0000): PMT PIDs are learned
from the PAT, and elementary-stream PIDs from the PMTs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .diagnostics import (
    SECTION_CRC_ERROR,
    SECTION_INCOMPLETE,
    SECTION_MALFORMED,
    TABLE_VERSION_SWITCH,
    Disposition,
    Finding,
    Severity,
)

PAT_PID = 0x0000
PAT_TABLE_ID = 0x00
PMT_TABLE_ID = 0x02
MAX_SECTION_LENGTH = 1024  # incl. CRC; section_length field limit is 0xFFD


# ---------------------------------------------------------------------------
# CRC32/MPEG-2 (poly 0x04C11DB7, init 0xFFFFFFFF, no reflect, no final xor)
# ---------------------------------------------------------------------------
def crc32_mpeg2(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


# ---------------------------------------------------------------------------
# Parsed table structures
# ---------------------------------------------------------------------------
@dataclass
class StreamInfo:
    elementary_pid: int
    stream_type: int
    descriptors: bytes = b""


@dataclass
class ProgramInfo:
    program_number: int
    pmt_pid: int
    version: int
    streams: list[StreamInfo] = field(default_factory=list)
    pcr_pid: int | None = None


@dataclass
class PatMap:
    version: int
    # program_number -> pmt_pid ; program 0 -> network_pid is kept separately
    programs: dict[int, int]
    network_pid: int | None
    transport_stream_id: int


@dataclass
class _RawSection:
    table_id: int
    body: bytes  # full section incl. header and CRC


# ---------------------------------------------------------------------------
# Section assembler
# ---------------------------------------------------------------------------
class SectionAssembler:
    """Reassemble MPEG-2 sections for one section-bearing PID.

    Model: a per-PID linear byte stream that always points at the start of
    the next section (or is empty).  The pointer_field on a
    payload-unit-start packet tells us the exact offset where new section
    bytes begin in that packet; everything after the last section in a
    packet is TS stuffing and is never placed in the buffer.

    Buffering rule when a section spans packets: store only the genuine
    section bytes present in the packet (i.e. up to the declared length),
    never the packet-end 0xFF stuffing.  Continuation packets resume the
    section directly at their first payload byte.
    """

    def __init__(self, pid: int, packet_index_of: Callable[[], int],
                 max_section_bytes: int = MAX_SECTION_LENGTH) -> None:
        self.pid = pid
        self._packet_index_of = packet_index_of
        self.max_section_bytes = max_section_bytes
        self._buf = bytearray()        # prefix of the section in flight
        self._total: int | None = None  # declared total, None if <3 bytes

    def _finding(self, code, severity, disposition, message, **details):
        return Finding(code=code, severity=severity, disposition=disposition,
                       message=message,
                       packet_index=self._packet_index_of(), pid=self.pid,
                       details=details)

    def _reset(self) -> None:
        self._buf = bytearray()
        self._total = None

    def _length_of(self, first3: bytes) -> int | None:
        sl = ((first3[1] & 0x0F) << 8) | first3[2]
        total = 3 + sl
        if sl < 1 or total > self.max_section_bytes:
            return None
        return total

    def _verify(self, section: bytes):
        table_id = section[0]
        expect = int.from_bytes(section[-4:], "big")
        actual = crc32_mpeg2(section[:-4])
        if actual != expect:
            return self._finding(
                SECTION_CRC_ERROR, Severity.ERROR, Disposition.REJECTED,
                f"CRC mismatch on PID {self.pid:#06x} "
                f"(table_id=0x{table_id:02x}, {len(section)} bytes)",
                table_id=table_id, section_length=len(section) - 3,
                expected_crc=f"0x{expect:08x}",
                actual_crc=f"0x{actual:08x}")
        return _RawSection(table_id=table_id, body=bytes(section))

    # ------------------------------------------------------------------
    def feed(self, payload: bytes, pusi: bool) -> list:
        if pusi:
            return self._feed_pusi(payload)
        return self._feed_continuation(payload)

    def _feed_pusi(self, payload: bytes) -> list:
        out: list = []
        if not payload:
            if self._buf:
                out.append(self._finding(
                    SECTION_MALFORMED, Severity.ERROR, Disposition.REJECTED,
                    "payload-unit-start with no payload; discarding partial "
                    "section"))
                self._reset()
            return out
        pointer = payload[0]
        if pointer > len(payload) - 1:
            out.append(self._finding(
                SECTION_MALFORMED, Severity.ERROR, Disposition.REJECTED,
                f"pointer_field {pointer} overruns payload"))
            self._reset()
            return out

        # 1) `pointer` bytes finish the in-flight section.
        if self._buf and pointer > 0:
            tail = payload[1:1 + pointer]
            self._buf.extend(tail)
            if self._total is None and len(self._buf) >= 3:
                self._total = self._length_of(bytes(self._buf[:3]))
            if self._total is not None:
                if len(self._buf) == self._total:
                    out.append(self._verify(bytes(self._buf)))
                    self._reset()
                elif len(self._buf) > self._total:
                    out.append(self._finding(
                        SECTION_MALFORMED, Severity.ERROR,
                        Disposition.REJECTED,
                        "section exceeded its declared length"))
                    self._reset()
                    return out
                else:
                    out.append(self._finding(
                        SECTION_MALFORMED, Severity.ERROR,
                        Disposition.REJECTED,
                        "pointer_field ended before declared section length"))
                    self._reset()
                    return out
        elif pointer > 0 and not self._buf:
            # No valid section in flight: pointer bytes are unreachable.
            pass

        # 2) Walk new sections starting exactly at 1+pointer. Bytes after the
        #    last complete section in this packet are stuffing (0xFF run, or
        #    the prefix of a section that continues).
        pos = 1 + pointer
        end = len(payload)
        while pos < end:
            if payload[pos] == 0xFF:
                break  # TS stuffing
            if end - pos < 3:
                # Fewer than the 3 header bytes remain inside this TS
                # packet. Strip trailing 0xFF TS stuffing before recording
                # the genuine (1-2 byte) header prefix. The ambiguity with a
                # section_length low byte of 0xFF only arises with >=3 bytes
                # available and is handled by the declared-length path.
                tail = bytes(payload[pos:end]).rstrip(b"\xff")
                self._buf = bytearray(tail)
                self._total = None
                break
            parsed_total = self._length_of(payload[pos:pos + 3])
            total = parsed_total
            if total is not None and pos + total > end:
                # Unusual header-straddle: 1-2 genuine header bytes followed
                # by packet-end 0xFF stuffing. Detect by an all-0xFF tail.
                for keep in (2, 1):
                    after = bytes(payload[pos + keep:end])
                    if after and all(b == 0xFF for b in after):
                        self._buf = bytearray(payload[pos:pos + keep])
                        self._total = None
                        total = -1  # signal: partial header buffered, stop
                        break
            if total == -1:
                break
            if total is None:
                sl = ((payload[pos + 1] & 0x0F) << 8) | payload[pos + 2]
                out.append(self._finding(
                    SECTION_MALFORMED, Severity.ERROR, Disposition.REJECTED,
                    f"invalid section_length {sl} on PID {self.pid:#06x}",
                    section_length=sl))
                self._reset()
                return out
            if pos + total <= end:
                out.append(self._verify(payload[pos:pos + total]))
                pos += total
                continue
            # Cross-packet.  This packet contributes at most `end-pos`
            # section bytes; the packet tail is 0xFF TS stuffing.  Because a
            # section prefix may itself contain 0xFF, we cannot strip by
            # value -- but the continuation packet starts a fresh 184-byte
            # payload, so we simply remember how many prefix bytes this PUSI
            # contributed and expect continuations to resume directly.
            # Everything pos..end IS prefix here (packet fully filled).
            self._buf = bytearray(payload[pos:end])
            self._total = total
            break
        return out

    def _feed_continuation(self, payload: bytes) -> list:
        out: list = []
        if not self._buf:
            return out  # duplicate / unsolicited continuation
        data = payload
        if self._total is None:
            need = 3 - len(self._buf)
            self._buf.extend(data[:need])
            data = data[need:]
            if len(self._buf) < 3:
                return out
            total = self._length_of(bytes(self._buf[:3]))
            if total is None:
                out.append(self._finding(
                    SECTION_MALFORMED, Severity.ERROR,
                    Disposition.REJECTED,
                    "invalid section_length in continued header"))
                self._reset()
                return out
            self._total = total
        need = self._total - len(self._buf)
        self._buf.extend(data[:need])
        if len(self._buf) >= self._total:
            if len(self._buf) == self._total:
                out.append(self._verify(bytes(self._buf)))
            self._reset()
        return out

    def signal_gap(self):
        had = bool(self._buf)
        self._reset()
        if not had:
            return None
        return self._finding(
            SECTION_MALFORMED, Severity.ERROR, Disposition.REJECTED,
            "partial section discarded due to transport-layer "
            "packet loss/discontinuity")

    def flush_end(self) -> list:
        out = []
        if self._buf:
            out.append(self._finding(
                SECTION_INCOMPLETE, Severity.ERROR, Disposition.UNDETERMINED,
                f"unterminated section byte(s) remain on PID {self.pid:#06x} "
                "at end of input; section cannot be validated",
                buffered_bytes=len(self._buf)))
        self._reset()
        return out


# ---------------------------------------------------------------------------
# PAT / PMT parsing
# ---------------------------------------------------------------------------
def _table_header(section: bytes) -> tuple[int, int, int, int, int]:
    """Return (table_id_extension, version, current_next, section_number,
    last_section_number) from a long-header section."""
    ext = int.from_bytes(section[3:5], "big")
    version = (section[5] >> 1) & 0x1F
    cni = section[5] & 0x01
    num = section[6]
    last = section[7]
    return ext, version, cni, num, last


def parse_pat(section: bytes) -> tuple[PatMap, dict[int, int], int]:
    """Parse one PAT section.

    Returns (program entries of this section, network entries, tsid).
    Program entries map program_number -> pmt_pid.
    """
    tsid, _version, _cni, _num, _last = _table_header(section)
    programs: dict[int, int] = {}
    network: dict[int, int] = {}
    pos = 8
    end = len(section) - 4  # exclude CRC
    while pos + 4 <= end:
        prog = int.from_bytes(section[pos:pos + 2], "big")
        pid = ((section[pos + 2] & 0x1F) << 8) | section[pos + 3]
        pos += 4
        if prog == 0:
            network[0] = pid
        elif prog != 0xFFFF:  # 0xFFFF reserved placeholder
            programs[prog] = pid
    # A well-formed PAT section carries no descriptors; remaining bytes would
    # be malformed, and are treated as such.
    if pos != end:
        raise ValueError("PAT section has trailing bytes after program loop")
    net_pid = network.get(0)
    return (PatMap(version=_version, programs=programs,
                   network_pid=net_pid, transport_stream_id=tsid),
            network, tsid)


def parse_pmt(section: bytes) -> tuple[int, int, list[StreamInfo]]:
    """Parse one PMT section -> (program_number, pcr_pid, streams)."""
    program_number, _version, _cni, _num, _last = _table_header(section)
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    pos = 12 + program_info_length
    if pos > len(section) - 4:
        raise ValueError("program_info_length overruns PMT section")
    end = len(section) - 4
    streams: list[StreamInfo] = []
    while pos + 5 <= end:
        stream_type = section[pos]
        elem_pid = ((section[pos + 1] & 0x1F) << 8) | section[pos + 2]
        es_info_len = ((section[pos + 3] & 0x0F) << 8) | section[pos + 4]
        pos += 5
        if pos + es_info_len > end:
            raise ValueError("ES_info_length overruns PMT section")
        desc = bytes(section[pos:pos + es_info_len])
        streams.append(StreamInfo(elementary_pid=elem_pid,
                                  stream_type=stream_type,
                                  descriptors=desc))
        pos += es_info_len
    if pos != end:
        raise ValueError("PMT section has trailing bytes after stream loop")
    return program_number, pcr_pid, streams


# ---------------------------------------------------------------------------
# Tables manager: staging + atomic activation
# ---------------------------------------------------------------------------
@dataclass
class _PatStage:
    version: int
    tsid: int
    sections: dict[int, bytes]
    last_section: int
    programs: dict[int, int]
    network_pid: int | None


class TablesManager:
    """Owns the active program map and per-PID section assemblers."""

    def __init__(self, max_section_bytes: int = MAX_SECTION_LENGTH) -> None:
        self.max_section_bytes = max_section_bytes
        self.pat: PatMap | None = None
        self.programs: dict[int, ProgramInfo] = {}
        self.pmt_pids: set[int] = set()
        self.network_pid: int | None = None
        self.history: list[Finding] = []  # version-switch audit trail

        self._assemblers: dict[int, SectionAssembler] = {}
        self._pat_stage: _PatStage | None = None
        # pmt pid -> staged (program_number, version, sections, last, streams)
        self._pmt_stage: dict[int, dict[str, object]] = {}
        self._current_packet_index = -1

    # -- wiring -------------------------------------------------------------
    def _assembler(self, pid: int) -> SectionAssembler:
        asm = self._assemblers.get(pid)
        if asm is None:
            asm = SectionAssembler(
                pid, lambda: self._current_packet_index,
                max_section_bytes=self.max_section_bytes)
            self._assemblers[pid] = asm
        return asm

    def signal_gap(self, pid: int) -> list[Finding]:
        asm = self._assemblers.get(pid)
        if asm is None:
            return []
        f = asm.signal_gap()
        return [f] if f else []

    def known_section_pid(self, pid: int) -> bool:
        return pid == PAT_PID or pid in self.pmt_pids

    def flush_end(self) -> list[Finding]:
        out: list[Finding] = []
        for asm in self._assemblers.values():
            out.extend(asm.flush_end())
        return out

    # -- feed ---------------------------------------------------------------
    def reset_partial(self) -> list[Finding]:
        """Discard in-flight (uncommitted) section bytes after a resync.

        Committed PAT/PMT maps are retained -- a resync does not erase the
        program information already validated.  Partial sections are dropped
        silently as malformed fragments; their loss is already recorded by
        the resync finding itself.
        """
        for asm in self._assemblers.values():
            asm.signal_gap()
        self._pat_stage = None
        self._pmt_stage.clear()
        return []

    def feed_packet(self, pid: int, payload: bytes, pusi: bool,
                    packet_index: int) -> list[Finding]:
        self._current_packet_index = packet_index
        findings: list[Finding] = []
        asm = self._assembler(pid)
        for item in asm.feed(payload, pusi):
            if isinstance(item, Finding):
                findings.append(item)
                # CRC/loss errors invalidate the in-flight staged version.
                if pid == PAT_PID:
                    self._pat_stage = None
                else:
                    self._pmt_stage.pop(pid, None)
                continue
            if pid == PAT_PID:
                findings.extend(self._on_pat_section(item))
            elif pid in self.pmt_pids:
                findings.extend(self._on_pmt_section(pid, item))
            # Sections on PIDs not currently mapped as PMT are ignored
            # (unknown PID handled separately by the analyzer).
        return findings

    # -- PAT -----------------------------------------------------------------
    def _on_pat_section(self, sec: _RawSection) -> list[Finding]:
        if sec.table_id != PAT_TABLE_ID:
            return [Finding(
                code=SECTION_MALFORMED, severity=Severity.ERROR,
                disposition=Disposition.REJECTED,
                message=f"unexpected table_id 0x{sec.table_id:02x} on PAT PID",
                packet_index=self._current_packet_index, pid=PAT_PID,
                details={"table_id": sec.table_id})]
        try:
            pat, _net, tsid = parse_pat(sec.body)
        except (ValueError, IndexError) as exc:
            return [Finding(
                code=SECTION_MALFORMED, severity=Severity.ERROR,
                disposition=Disposition.REJECTED,
                message=f"malformed PAT section rejected: {exc}",
                packet_index=self._current_packet_index, pid=PAT_PID)]

        ext, version, _cni, number, last = _table_header(sec.body)
        stage = self._pat_stage
        if stage is None or stage.version != version:
            stage = _PatStage(version=version, tsid=tsid, sections={},
                              last_section=last,
                              programs={}, network_pid=None)
            self._pat_stage = stage
        stage.sections[number] = sec.body

        # Merge this section's entries into the staged map.
        stage.programs.update(pat.programs)
        if pat.network_pid is not None:
            stage.network_pid = pat.network_pid

        if set(stage.sections) != set(range(last + 1)):
            return []  # wait for the remaining sections of this version

        # All sections present and individually CRC-verified -> atomic commit.
        return self._commit_pat(stage)

    def _commit_pat(self, stage: _PatStage) -> list[Finding]:
        findings: list[Finding] = []
        old_version = self.pat.version if self.pat else None
        if old_version is not None and old_version != stage.version:
            findings.append(Finding(
                code=TABLE_VERSION_SWITCH, severity=Severity.INFO,
                disposition=Disposition.ACCEPTED,
                message=f"PAT version {old_version} -> {stage.version}; "
                        "program map replaced atomically",
                packet_index=self._current_packet_index, pid=PAT_PID,
                details={"old_version": old_version,
                         "new_version": stage.version,
                         "programs": sorted(stage.programs)}))
        new_map = PatMap(version=stage.version,
                         programs=dict(stage.programs),
                         network_pid=stage.network_pid,
                         transport_stream_id=stage.tsid)
        self.pat = new_map
        self.network_pid = stage.network_pid
        self._pat_stage = None

        # Reconcile PMT assemblers with the new program->PMT-PID map in the
        # same atomic operation.
        new_pmt_pids = set(stage.programs.values())
        for gone in self.pmt_pids - new_pmt_pids:
            self._assemblers.pop(gone, None)
            self._pmt_stage.pop(gone, None)
            # Programs whose PMT PID disappeared are removed atomically too.
            for prog in [p for p, info in self.programs.items()
                         if info.pmt_pid == gone]:
                del self.programs[prog]
        self.pmt_pids = new_pmt_pids
        return findings

    # -- PMT -----------------------------------------------------------------
    def _on_pmt_section(self, pid: int, sec: _RawSection) -> list[Finding]:
        if sec.table_id != PMT_TABLE_ID:
            return [Finding(
                code=SECTION_MALFORMED, severity=Severity.ERROR,
                disposition=Disposition.REJECTED,
                message=f"unexpected table_id 0x{sec.table_id:02x} on "
                        f"PMT PID {pid:#06x}",
                packet_index=self._current_packet_index, pid=pid,
                details={"table_id": sec.table_id})]
        try:
            program_number, pcr_pid, streams = parse_pmt(sec.body)
        except (ValueError, IndexError) as exc:
            return [Finding(
                code=SECTION_MALFORMED, severity=Severity.ERROR,
                disposition=Disposition.REJECTED,
                message=f"malformed PMT section rejected: {exc}",
                packet_index=self._current_packet_index, pid=pid)]

        ext, version, _cni, number, last = _table_header(sec.body)
        stage = self._pmt_stage.get(pid)
        if stage is None or stage["version"] != version:
            stage = {"program_number": program_number, "version": version,
                     "sections": {}, "last": last,
                     "streams": [], "pcr_pid": pcr_pid, "section_numbers": []}
            self._pmt_stage[pid] = stage
        sections: dict[int, bytes] = stage["sections"]  # type: ignore[assignment]
        sections[number] = sec.body

        # Streams accumulate in section order; single-section PMTs are the
        # common case and simply replace on commit.
        stage["streams"] = stage["streams"] + streams  # type: ignore[operator]

        if set(sections) != set(range(last + 1)):
            return []
        return self._commit_pmt(pid, stage)

    def _commit_pmt(self, pid: int, stage: dict[str, object]) -> list[Finding]:
        version = int(stage["version"])
        program_number = int(stage["program_number"])
        streams: list[StreamInfo] = stage["streams"]  # type: ignore[assignment]
        pcr_pid = stage["pcr_pid"]

        old = self.programs.get(program_number)
        findings: list[Finding] = []
        if old is not None and old.version != version:
            findings.append(Finding(
                code=TABLE_VERSION_SWITCH, severity=Severity.INFO,
                disposition=Disposition.ACCEPTED,
                message=f"PMT version {old.version} -> {version} for program "
                        f"{program_number}; stream map replaced atomically",
                packet_index=self._current_packet_index, pid=pid,
                details={"program_number": program_number,
                         "old_version": old.version,
                         "new_version": version,
                         "stream_pids": [s.elementary_pid for s in streams]}))

        # Defensive: the PMT PID carrying this program must match the PAT.
        pmt_pid = None
        if self.pat is not None:
            pmt_pid = self.pat.programs.get(program_number)
        effective_pmt_pid = pmt_pid if pmt_pid is not None else pid
        self.programs[program_number] = ProgramInfo(
            program_number=program_number,
            pmt_pid=effective_pmt_pid,
            version=version,
            streams=list(streams),
            pcr_pid=pcr_pid if isinstance(pcr_pid, int) else None,
        )
        self._pmt_stage.pop(pid, None)
        return findings

    # -- queries ------------------------------------------------------------
    def elementary_pids(self) -> dict[int, tuple[int, int]]:
        """elementary_pid -> (program_number, stream_type)."""
        out: dict[int, tuple[int, int]] = {}
        for prog in self.programs.values():
            for s in prog.streams:
                out[s.elementary_pid] = (prog.program_number, s.stream_type)
        return out
