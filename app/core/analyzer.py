"""Top-level analysis orchestration.

Wires the independent kernels together in packet order:

  framing (sync) -> continuity (per PID) -> PSI tables / PES reassembly
                                            -> timing/PCR collection

The analyzer itself holds no parsing rules; each decision comes from a kernel
and is surfaced verbatim as a :class:`Finding` with packet index, PID and the
state that motivated it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from . import diagnostics as diag
from .continuity import NULL_PID, ContinuityChecker
from .diagnostics import (
    NO_SYNC,
    TEI_SET,
    TRAILING_BYTES,
    UNKNOWN_PID,
    Disposition,
    Finding,
    Severity,
)
from .pes import PesAssembler, PesFragment
from .psi import PAT_PID, PatMap, ProgramInfo, TablesManager
from .sync import PacketScanner
from .timing import PidTiming, TimingCollector


@dataclass
class PidActivity:
    pid: int
    packets: int = 0
    payload_packets: int = 0
    role: str = "unknown"          # pat | pmt | elementary | network | null
    stream_type: int | None = None
    program_number: int | None = None
    scrambled_packets: int = 0


@dataclass
class AnalysisReport:
    request_id: str
    verdict: str                   # accepted | undetermined | rejected
    total_bytes: int
    parsed_packets: int
    resyncs: int
    skipped_bytes: int
    trailing_bytes: int
    pat: PatMap | None
    programs: dict[int, ProgramInfo]
    pid_activity: dict[int, PidActivity]
    pes: dict[int, list[PesFragment]]
    pes_gap_count: int
    timing: dict[int, PidTiming]
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        def pat_dict(p: PatMap | None) -> object:
            if p is None:
                return None
            return {
                "version": p.version,
                "transport_stream_id": p.transport_stream_id,
                "network_pid": p.network_pid,
                "programs": {str(k): v for k, v in p.programs.items()},
            }

        return {
            "request_id": self.request_id,
            "verdict": self.verdict,
            "total_bytes": self.total_bytes,
            "parsed_packets": self.parsed_packets,
            "resyncs": self.resyncs,
            "skipped_bytes": self.skipped_bytes,
            "trailing_bytes": self.trailing_bytes,
            "pat": pat_dict(self.pat),
            "programs": {
                str(num): {
                    "program_number": info.program_number,
                    "pmt_pid": info.pmt_pid,
                    "version": info.version,
                    "pcr_pid": info.pcr_pid,
                    "streams": [
                        {
                            "elementary_pid": s.elementary_pid,
                            "stream_type": s.stream_type,
                        }
                        for s in info.streams
                    ],
                }
                for num, info in sorted(self.programs.items())
            },
            "pid_activity": {
                str(pid): {
                    "packets": a.packets,
                    "payload_packets": a.payload_packets,
                    "role": a.role,
                    "stream_type": a.stream_type,
                    "program_number": a.program_number,
                    "scrambled_packets": a.scrambled_packets,
                }
                for pid, a in sorted(self.pid_activity.items())
            },
            "pes": {
                str(pid): [
                    {
                        "start_packet_index": f.start_packet_index,
                        "end_packet_index": f.end_packet_index,
                        "stream_id": f.header.stream_id if f.header else None,
                        "pes_length": f.header.pes_length if f.header else None,
                        "pts": f.header.pts if f.header else None,
                        "dts": f.header.dts if f.header else None,
                        "bytes": len(f.data),
                        "corrupt": f.corrupt,
                        "corrupt_reason": f.corrupt_reason,
                        "gap_packets": f.gap_packets,
                    }
                    for f in frags
                ]
                for pid, frags in sorted(self.pes.items())
            },
            "pes_gap_count": self.pes_gap_count,
            "timing": {str(pid): t.to_dict()
                       for pid, t in sorted(self.timing.items())},
            "findings": [f.to_dict() for f in self.findings],
        }


def analyze(data: bytes, request_id: str,
            settings: Settings | None = None) -> AnalysisReport:
    settings = settings or Settings()
    findings: list[Finding] = []

    checker = ContinuityChecker()
    tables = TablesManager(max_section_bytes=settings.max_section_bytes)
    timing = TimingCollector()
    assemblers: dict[int, PesAssembler] = {}
    activity: dict[int, PidActivity] = {}

    parsed_packets = 0
    resyncs = 0
    skipped_bytes = 0
    trailing_bytes = 0
    had_lock = False

    def role_for(pid: int, elementary: dict[int, tuple[int, int]]) -> tuple[str, int | None, int | None]:
        if pid == PAT_PID:
            return "pat", None, None
        if pid == tables.network_pid:
            return "network", None, None
        if pid in tables.pmt_pids:
            return "pmt", None, None
        if pid in elementary:
            prog, stype = elementary[pid]
            return "elementary", stype, prog
        return "unknown", None, None

    previous_raw: dict[int, bytes] = {}
    warned_unknown: set[int] = set()
    warned_scrambled: set[int] = set()

    for event in PacketScanner(data).events():
        if event.kind == "resync":
            resyncs += 1
            skipped_bytes += event.skipped_bytes
            # All per-PID stream state is unreliable across a sync loss:
            # reset continuity baselines and discard partial PSI/PES bytes.
            checker.reset()
            findings.extend(tables.reset_partial())
            for asm in assemblers.values():
                asm.reset(packet_index=-1)
            previous_raw.clear()
            findings.append(Finding(
                code=diag.RESYNC_OCCURRED,
                severity=Severity.WARNING,
                disposition=Disposition.UNDETERMINED,
                message=f"lost packet lock; recovered after "
                        f"{event.skipped_bytes} byte(s) "
                        f"(offset {event.from_offset}..{event.to_offset})",
                details={
                    "skipped_bytes": event.skipped_bytes,
                    "from_offset": event.from_offset,
                    "to_offset": event.to_offset,
                }))
            continue

        if event.kind == "trailing":
            trailing_bytes += event.trailing_bytes
            code = NO_SYNC if not had_lock else TRAILING_BYTES
            sev = Severity.ERROR if not had_lock else Severity.WARNING
            disp = Disposition.UNDETERMINED
            findings.append(Finding(
                code=code, severity=sev, disposition=disp,
                message=("no MPEG-TS sync found in input"
                         if not had_lock
                         else f"{event.trailing_bytes} trailing byte(s) not "
                              "aligned to a 188-byte packet"),
                details={"bytes": event.trailing_bytes,
                         "from_offset": event.from_offset}))
            continue

        pkt = event.packet
        assert pkt is not None
        had_lock = True
        parsed_packets += 1

        act = activity.get(pkt.pid)
        if act is None:
            act = PidActivity(pid=pkt.pid)
            activity[pkt.pid] = act
        act.packets += 1
        if pkt.has_payload:
            act.payload_packets += 1
        if pkt.tsc != 0:
            act.scrambled_packets += 1

        # --- continuity ----------------------------------------------------
        verdict = checker.check(pkt)
        if verdict.kind == "duplicate":
            equal = previous_raw.get(pkt.pid) == pkt.raw
            checker.note_bytes_equal(verdict, equal)
        if verdict.finding is not None:
            findings.append(verdict.finding)
        previous_raw[pkt.pid] = pkt.raw

        is_duplicate = verdict.kind == "duplicate"
        is_loss = verdict.kind in ("gap", "signaled_discontinuity")
        is_bad_adaptation = verdict.kind == "adaptation_cc_mismatch"

        # --- transport error indicator ------------------------------------
        tei = pkt.tei
        if tei:
            findings.append(Finding(
                code=TEI_SET,
                severity=Severity.ERROR,
                disposition=Disposition.UNDETERMINED,
                message=f"transport_error_indicator set on PID "
                        f"{pkt.pid:#06x}; payload trust cannot be decided",
                packet_index=pkt.index, pid=pkt.pid,
                details={"cc": pkt.continuity_counter}))

        # --- table PIDs ----------------------------------------------------
        if tables.known_section_pid(pkt.pid) and pkt.has_payload:
            if is_loss or is_bad_adaptation or tei:
                for f in tables.signal_gap(pkt.pid):
                    findings.append(f)
            elif pkt.tsc == 0 and not is_duplicate:
                for f in tables.feed_packet(
                        pkt.pid, pkt.payload, pkt.pusi, pkt.index):
                    findings.append(f)

        # --- PCR -----------------------------------------------------------
        if pkt.adaptation is not None and pkt.adaptation.pcr is not None:
            timing.record_pcr(
                pkt.pid, pkt.index, pkt.byte_offset,
                pkt.adaptation.pcr,
                signaled_discontinuity=bool(
                    pkt.adaptation.discontinuity
                    or verdict.kind == "signaled_discontinuity"))

        # --- elementary / unknown PIDs ------------------------------------
        elementary = tables.elementary_pids()
        if pkt.pid != NULL_PID:
            role, stype, prog = role_for(pkt.pid, elementary)
            act.role = role
            act.stream_type = stype
            act.program_number = prog

        if pkt.pid in elementary and pkt.has_payload and pkt.tsc == 0:
            asm = assemblers.get(pkt.pid)
            if asm is None:
                prog_num, st = elementary[pkt.pid]
                asm = PesAssembler(pkt.pid, st, prog_num,
                                   settings.max_pes_bytes)
                assemblers[pkt.pid] = asm
            if is_loss:
                asm.signal_loss(pkt.index, verdict.missing_packets,
                                verdict.signaled)
            elif tei:
                asm.signal_tei(pkt.index)
            else:
                asm.feed(pkt.payload, pkt.pusi, is_duplicate, pkt.index)
        elif (pkt.pid in elementary and pkt.has_payload and pkt.tsc != 0
              and pkt.pid not in warned_scrambled):
            warned_scrambled.add(pkt.pid)
            findings.append(Finding(
                code=diag.PES_SCRAMBLED,
                severity=Severity.WARNING,
                disposition=Disposition.UNDETERMINED,
                message=f"scrambled payload on elementary PID "
                        f"{pkt.pid:#06x} (tsc={pkt.tsc}); not reassembled",
                packet_index=pkt.index, pid=pkt.pid,
                details={"transport_scrambling_control": pkt.tsc}))
        elif (pkt.pid != NULL_PID and pkt.has_payload and pkt.tsc == 0
              and not tables.known_section_pid(pkt.pid)
              and pkt.pid not in elementary
              and pkt.pid not in warned_unknown
              and not tei):
            warned_unknown.add(pkt.pid)
            findings.append(Finding(
                code=UNKNOWN_PID,
                severity=Severity.WARNING,
                disposition=Disposition.UNDETERMINED,
                message=f"payload on PID {pkt.pid:#06x} not referenced by any "
                        "PAT/PMT seen so far; content cannot be classified",
                packet_index=pkt.index, pid=pkt.pid,
                details={"cc": pkt.continuity_counter}))

    # --- end of stream -----------------------------------------------------
    for f in tables.flush_end():
        findings.append(f)
    for asm in assemblers.values():
        asm.flush_end()
        findings.extend(asm.findings)
        for t in asm.timing:
            timing.record_pes_timing(t.pid, t.pts, t.dts)

    timing_summary, timing_findings = timing.summarize()
    findings.extend(timing_findings)

    # Refresh roles after final maps are committed.
    elementary = tables.elementary_pids()
    for pid, act in activity.items():
        role, stype, prog = role_for(pid, elementary)
        act.role = role
        act.stream_type = stype
        act.program_number = prog
    if NULL_PID in activity:
        activity[NULL_PID].role = "null"

    pes_out = {pid: asm.completed for pid, asm in assemblers.items()}
    pes_gap_count = sum(asm.gap_count for asm in assemblers.values())

    verdict_name = "accepted"
    if any(f.disposition == Disposition.REJECTED for f in findings):
        verdict_name = "rejected"
    elif any(f.severity == Severity.WARNING for f in findings):
        verdict_name = "undetermined"
    if not had_lock:
        verdict_name = "rejected"

    return AnalysisReport(
        request_id=request_id,
        verdict=verdict_name,
        total_bytes=len(data),
        parsed_packets=parsed_packets,
        resyncs=resyncs,
        skipped_bytes=skipped_bytes,
        trailing_bytes=trailing_bytes,
        pat=tables.pat,
        programs=tables.programs,
        pid_activity=activity,
        pes=pes_out,
        pes_gap_count=pes_gap_count,
        timing=timing_summary,
        findings=findings,
    )
