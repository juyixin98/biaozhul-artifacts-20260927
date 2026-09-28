"""Top-level stream analysis driver.

Pipeline over one byte buffer:

    sync framing -> header parse -> TEI/scramble gates
                 -> per-PID continuity check
                 -> PSI section reassembly (PAT/PMT)
                 -> PES reassembly (PMT-declared elementary PIDs)
                 -> PCR timing kernel

PID roles are never assumed: PMT PIDs come from the current PAT and
elementary stream PIDs come from current PMTs. Until a map exists those
PIDs only receive continuity/statistics treatment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .config import Settings
from .diagnostics import Code, DiagnosticsCollector, Severity
from .core.continuity import CCVerdict, ContinuityTracker
from .core.packets import (
    NULL_PID,
    PAT_PID,
    PacketHeader,
    PacketParseError,
    TS_PACKET_SIZE,
    parse_packet_header,
)
from .core.pes import PESRecord, PesAssembler
from .core.sync import find_sync
from .core.tables import ProgramMapStore, TableAssembler
from .core.timing import TimingKernel


@dataclass
class PidStats:
    packets: int = 0
    payload_packets: int = 0
    tei: int = 0
    scrambled: int = 0


@dataclass
class FramingStats:
    packets_parsed: int = 0
    bytes_skipped: int = 0
    sync_events: int = 0
    leftover_bytes: int = 0
    fatal: Optional[str] = None


@dataclass
class AnalysisResult:
    diagnostics: DiagnosticsCollector
    framing: FramingStats = field(default_factory=FramingStats)
    pid_stats: dict[int, PidStats] = field(default_factory=dict)
    programs: Optional[ProgramMapStore] = None
    continuity: Optional[ContinuityTracker] = None
    pes: Optional[PesAssembler] = None
    timing: Optional[TimingKernel] = None

    @property
    def ok(self) -> bool:
        return self.framing.fatal is None

    def report_dict(self, event_limit: int) -> dict:
        counts = self.diagnostics.counts_by_severity()
        events = self.diagnostics.events[:event_limit]
        cont_snapshot = {}
        for pid, state in self.continuity.snapshot().items():
            cont_snapshot[str(pid)] = {
                "packets": state.packets,
                "payload_packets": state.payload_packets,
                "duplicates": state.duplicates,
                "lost": state.lost,
                "stalls": state.stalls,
                "declared_discontinuities": state.declared_discontinuities,
            }
        return {
            "framing": {
                "packets_parsed": self.framing.packets_parsed,
                "bytes_skipped": self.framing.bytes_skipped,
                "sync_events": self.framing.sync_events,
                "leftover_bytes": self.framing.leftover_bytes,
                "fatal": self.framing.fatal,
            },
            "pid_stats": {
                str(pid): {
                    "packets": st.packets,
                    "payload_packets": st.payload_packets,
                    "transport_error_indicator": st.tei,
                    "scrambled": st.scrambled,
                }
                for pid, st in sorted(self.pid_stats.items())
            },
            "programs": self.programs.snapshot(),
            "continuity": cont_snapshot,
            "pes_records": [r.to_dict() for r in self.pes.records],
            "timing": {str(pid): stats for pid, stats in self.timing.snapshot().items()},
            "diagnostics": {
                "record_id": self.diagnostics.record_id,
                "counts": counts,
                "events_included": len(events),
                "events_truncated": max(0, len(self.diagnostics.events) - len(events)),
                "events": [e.to_dict() for e in events],
            },
        }


class StreamAnalyzer:
    def __init__(self, settings: Settings, record_id: str):
        self.settings = settings
        self.diag = DiagnosticsCollector(record_id)
        self.framing = FramingStats()
        self.pid_stats: dict[int, PidStats] = {}
        self.programs = ProgramMapStore(self.diag)
        self.tables = TableAssembler(
            self.diag, self.programs, settings.max_section_bytes
        )
        self.continuity = ContinuityTracker(self.diag)
        self.pes = PesAssembler(self.diag, settings.max_pes_payload_bytes)
        self.timing = TimingKernel(self.diag)
        self._synced = False

    # ------------------------------------------------------------------ #
    def analyze(self, data: bytes) -> AnalysisResult:
        length = len(data)
        pos = 0

        while pos + TS_PACKET_SIZE <= length:
            if not self._synced:
                found = find_sync(
                    data,
                    start=pos,
                    max_scan_bytes=self.settings.max_sync_scan_bytes,
                    confirm_packets=self.settings.sync_confirm_packets,
                )
                if found.offset is None:
                    self.framing.bytes_skipped += found.skipped_bytes
                    self.framing.fatal = (
                        f"sync recovery failed after scanning {found.scanned_bytes} bytes"
                    )
                    self.diag.error(
                        Code.SYNC_RECOVERY_FAILED,
                        "no confirmed 0x47 sync pattern within bounded scan window",
                        offset=pos,
                        scanned_bytes=found.scanned_bytes,
                        max_scan_bytes=self.settings.max_sync_scan_bytes,
                    )
                    pos = length
                    break
                if found.skipped_bytes:
                    self.framing.bytes_skipped += found.skipped_bytes
                    self.diag.warning(
                        Code.SYNC_RECOVERED,
                        "sync recovered after skipping non-packet bytes",
                        offset=found.offset,
                        skipped_bytes=found.skipped_bytes,
                    )
                    self.continuity.reset_after_sync_loss(found.offset)
                else:
                    self.diag.info(
                        Code.SYNC_LOCKED,
                        "initial TS sync locked",
                        offset=found.offset,
                    )
                self.framing.sync_events += 1
                self._synced = True
                pos = found.offset
                continue

            if data[pos] != 0x47:
                self.diag.error(
                    Code.SYNC_LOST,
                    "expected sync byte mid-stream; entering bounded rescan",
                    offset=pos,
                )
                self.framing.sync_events += 1
                self._synced = False
                continue

            try:
                header = parse_packet_header(data, pos)
            except PacketParseError as exc:
                self.diag.error(
                    Code.PACKET_PARSE_ERROR,
                    f"malformed packet, skipped: {exc}",
                    offset=pos,
                )
                # One bad packet structure: step one byte and resync.
                self._synced = False
                pos += 1
                continue

            self._dispatch(header, data)
            pos += TS_PACKET_SIZE

        self.framing.leftover_bytes = length - pos
        if self.framing.leftover_bytes:
            self.diag.info(
                Code.TRUNCATED_TAIL,
                "trailing bytes do not form a complete 188-byte packet",
                offset=length,
                leftover_bytes=self.framing.leftover_bytes,
            )
        self.tables.flush(length)
        self.pes.flush(length)

        return AnalysisResult(
            diagnostics=self.diag,
            framing=self.framing,
            pid_stats=self.pid_stats,
            programs=self.programs,
            continuity=self.continuity,
            pes=self.pes,
            timing=self.timing,
        )

    # ------------------------------------------------------------------ #
    def _stats(self, pid: int) -> PidStats:
        stats = self.pid_stats.get(pid)
        if stats is None:
            stats = PidStats()
            self.pid_stats[pid] = stats
        return stats

    def _dispatch(self, header: PacketHeader, data: bytes) -> None:
        self.framing.packets_parsed += 1
        pid = header.pid
        stats = self._stats(pid)
        stats.packets += 1
        if header.has_payload:
            stats.payload_packets += 1

        if header.tei:
            stats.tei += 1
            self.diag.error(
                Code.TRANSPORT_ERROR_INDICATOR,
                "packet flagged with transport_error_indicator; payload rejected",
                pid=pid,
                offset=header.offset,
            )

        payload = b""
        if header.has_payload:
            payload = bytes(data[header.payload_offset:header.payload_offset + header.payload_length])

        # Continuity is checked for every PID (including null packets' peers),
        # TEI or not, because the counter is a transport-layer signal.
        verdict = self.continuity.check(header, payload)

        if header.tei:
            return  # payload is corrupt: never feed reassembly or timing

        if pid == NULL_PID:
            return

        if header.is_scrambled:
            stats.scrambled += 1
            self.diag.info(
                Code.SCRAMBLED_PACKET,
                "scrambled packet; payload not interpreted",
                pid=pid,
                offset=header.offset,
                scrambling_control=header.scrambling_control,
            )
            return

        if verdict.verdict == CCVerdict.DUPLICATE:
            # Identical retransmission: do not append its payload a second
            # time to section/PES reassembly.
            return

        if verdict.verdict in (CCVerdict.LOST, CCVerdict.STALL):
            psi_pids = self.tables.psi_pids()
            if pid in psi_pids:
                self.tables.reset_on_gap(pid, header.offset)
            if pid in self.programs.es_pids():
                self.pes.mark_gap(pid, header.offset)

        af = header.adaptation_field
        if af is not None and af.pcr is not None:
            self.timing.add_pcr(pid, af.pcr, header.offset)

        if not header.has_payload:
            return

        psi_pids = self.tables.psi_pids()
        es_pids = self.programs.es_pids()
        if pid in psi_pids:
            self.tables.feed(pid, header.pusi, payload, header.offset)
        elif pid in es_pids:
            closed = self.pes.feed(pid, header.pusi, payload, header.offset)
            for _record in closed:
                pass  # records are retained on the assembler for reporting
        # Unknown PIDs: stats/continuity only, never guessed.
