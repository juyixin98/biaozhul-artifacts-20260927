"""Atomic PSI table management.

A new PAT/PMT version becomes visible only once a complete,
CRC-verified, ``current_next == 1`` section has been parsed. Repeated
sections of the same version are normal retransmission noise and do not
mutate the map. A version switch is one atomic replacement and one
diagnostic event carrying both the old and the new state — callers can
never observe a half-updated map (e.g. a new PAT whose PMTs are not yet
known, or a PMT whose streams are partially replaced).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..diagnostics import Code, DiagnosticsCollector
from .packets import PAT_PID
from .psi import (
    TABLE_ID_PAT,
    TABLE_ID_PMT,
    ParsedPAT,
    ParsedPMT,
    PATProgram,
    PMTStream,
    Section,
    SectionAssembler,
    parse_pat,
    parse_pmt,
)


@dataclass(frozen=True)
class CurrentPAT:
    version: int
    transport_stream_id: int
    programs: tuple[PATProgram, ...]


@dataclass(frozen=True)
class CurrentPMT:
    version: int
    program_number: int
    pcr_pid: Optional[int]
    streams: tuple[PMTStream, ...]


class ProgramMapStore:
    """Holds the current PAT and the current PMT per PMT PID atomically."""

    def __init__(self, diagnostics: DiagnosticsCollector):
        self._diag = diagnostics
        self._pat: Optional[CurrentPAT] = None
        self._pmts: dict[int, CurrentPMT] = {}

    @property
    def pat(self) -> Optional[CurrentPAT]:
        return self._pat

    @property
    def pmts(self) -> dict[int, CurrentPMT]:
        return dict(self._pmts)

    def pmt_pids(self) -> set[int]:
        if self._pat is None:
            return set()
        return {
            prog.pid
            for prog in self._pat.programs
            if prog.program_number != 0  # program 0 -> network PID, not a PMT
        }

    def es_pids(self) -> dict[int, int]:
        """elementary stream PID -> stream_type across every active PMT."""
        out: dict[int, int] = {}
        for pmt in self._pmts.values():
            for stream in pmt.streams:
                out[stream.pid] = stream.stream_type
        return out

    def pcr_pids(self) -> set[int]:
        return {pmt.pcr_pid for pmt in self._pmts.values() if pmt.pcr_pid is not None}

    def apply_pat(self, section: Section, offset: int) -> bool:
        parsed: ParsedPAT = parse_pat(section)
        if not parsed.current_next:
            self._diag.info(
                Code.TABLE_NOT_CURRENT,
                "PAT section has current_next_indicator=0; not applied",
                pid=PAT_PID,
                offset=offset,
                version=parsed.version,
            )
            return False

        current = CurrentPAT(
            version=parsed.version,
            transport_stream_id=parsed.transport_stream_id,
            programs=tuple(sorted(parsed.programs, key=lambda p: p.program_number)),
        )
        if (
            self._pat is not None
            and self._pat.version == current.version
            and self._pat.programs == current.programs
        ):
            self._diag.info(
                Code.PAT_REPEAT,
                "PAT version retransmitted unchanged",
                pid=PAT_PID,
                offset=offset,
                version=current.version,
            )
            return False

        old_programs = (
            tuple((p.program_number, p.pid) for p in self._pat.programs)
            if self._pat is not None
            else None
        )
        old_version = self._pat.version if self._pat is not None else None
        # Atomic publication: new PAT and the PMT set it references replace
        # the old configuration together. PMTs no longer referenced are
        # retired in the same step so callers never see an intermediate map.
        active_pmts = {p.pid for p in current.programs if p.program_number != 0}
        retired = sorted(pid for pid in self._pmts if pid not in active_pmts)
        for pid in retired:
            del self._pmts[pid]
        self._pat = current
        self._diag.info(
            Code.PAT_VERSION_SWITCH,
            "PAT updated atomically" if old_version is not None
            else "initial PAT applied atomically",
            pid=PAT_PID,
            offset=offset,
            old_version=old_version,
            new_version=current.version,
            old_programs=old_programs,
            new_programs=tuple((p.program_number, p.pid) for p in current.programs),
            retired_pmt_pids=retired,
        )
        return True

    def apply_pmt(self, pid: int, section: Section, offset: int) -> bool:
        parsed: ParsedPMT = parse_pmt(section)
        if not parsed.current_next:
            self._diag.info(
                Code.TABLE_NOT_CURRENT,
                "PMT section has current_next_indicator=0; not applied",
                pid=pid,
                offset=offset,
                version=parsed.version,
            )
            return False

        current = CurrentPMT(
            version=parsed.version,
            program_number=parsed.program_number,
            pcr_pid=parsed.pcr_pid,
            streams=tuple(sorted(parsed.streams, key=lambda s: s.pid)),
        )
        existing = self._pmts.get(pid)
        if (
            existing is not None
            and existing.version == current.version
            and existing.streams == current.streams
            and existing.pcr_pid == current.pcr_pid
        ):
            self._diag.info(
                Code.PMT_REPEAT,
                "PMT version retransmitted unchanged",
                pid=pid,
                offset=offset,
                version=current.version,
            )
            return False

        old_version = existing.version if existing is not None else None
        old_streams = (
            tuple((s.stream_type, s.pid) for s in existing.streams)
            if existing is not None
            else None
        )
        # Single assignment: the new stream set replaces the old one whole.
        self._pmts[pid] = current
        self._diag.info(
            Code.PMT_VERSION_SWITCH,
            "PMT updated atomically" if old_version is not None
            else "initial PMT applied atomically",
            pid=pid,
            offset=offset,
            program_number=current.program_number,
            old_version=old_version,
            new_version=current.version,
            old_streams=old_streams,
            new_streams=tuple((s.stream_type, s.pid) for s in current.streams),
            pcr_pid=current.pcr_pid,
        )
        return True

    def snapshot(self) -> dict:
        return {
            "pat": None
            if self._pat is None
            else {
                "version": self._pat.version,
                "transport_stream_id": self._pat.transport_stream_id,
                "programs": [
                    {"program_number": p.program_number, "pid": p.pid}
                    for p in self._pat.programs
                ],
            },
            "pmts": [
                {
                    "pid": pid,
                    "version": pmt.version,
                    "program_number": pmt.program_number,
                    "pcr_pid": pmt.pcr_pid,
                    "streams": [
                        {"pid": s.pid, "stream_type": s.stream_type}
                        for s in pmt.streams
                    ],
                }
                for pid, pmt in sorted(self._pmts.items())
            ],
        }


class TableAssembler:
    """Routes packet payloads to per-PID section assemblers and applies tables."""

    def __init__(
        self,
        diagnostics: DiagnosticsCollector,
        store: ProgramMapStore,
        max_section_bytes: int,
    ):
        self._diag = diagnostics
        self._store = store
        self._max_section_bytes = max_section_bytes
        self._assemblers: dict[int, SectionAssembler] = {}

    def psi_pids(self) -> set[int]:
        """PIDs currently carrying PSI: PID 0 plus every known PMT PID."""
        return {PAT_PID} | self._store.pmt_pids()

    def _assembler_for(self, pid: int) -> SectionAssembler:
        assembler = self._assemblers.get(pid)
        if assembler is None:
            assembler = SectionAssembler(pid, self._diag, self._max_section_bytes)
            self._assemblers[pid] = assembler
        return assembler

    def reset_on_gap(self, pid: int, offset: Optional[int]) -> None:
        self._assembler_for(pid).reset_on_gap(offset)

    def feed(
        self, pid: int, pusi: bool, payload: bytes, packet_offset: int
    ) -> None:
        assembler = self._assembler_for(pid)
        for section in assembler.feed(pusi, payload, packet_offset):
            self._apply(pid, section, packet_offset)

    def _apply(self, pid: int, section: Section, offset: int) -> None:
        if not section.crc_ok:
            self._diag.error(
                Code.TABLE_CRC_ERROR,
                "section rejected: CRC-32 mismatch",
                pid=pid,
                offset=offset,
                table_id=section.table_id,
                table_id_extension=section.table_id_extension,
                version=section.version,
                stored_crc=f"0x{section.stored_crc:08x}",
                computed_crc=f"0x{section.computed_crc:08x}",
            )
            return
        if pid == PAT_PID and section.table_id == TABLE_ID_PAT:
            self._store.apply_pat(section, offset)
            return
        if section.table_id == TABLE_ID_PMT:
            self._store.apply_pmt(pid, section, offset)
            return
        self._diag.info(
            Code.TABLE_UNSUPPORTED,
            "section accepted by CRC check but table type is not parsed",
            pid=pid,
            offset=offset,
            table_id=section.table_id,
        )

    def flush(self, end_offset: int) -> None:
        for assembler in self._assemblers.values():
            assembler.flush(end_offset)
