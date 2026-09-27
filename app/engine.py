"""Offline replay engine: run a whole packet trace through the jitter core.

Traces are synthetic fixtures (see :mod:`fixtures`), but the engine treats
them exactly as an online receiver would: packets are processed in arrival
order, and due items are drained after each arrival batch on a virtual
clock. A second run with a fixed delay provides the baseline the adaptive
algorithm is compared against.

Nothing here invents results: every metric is derived from ingest/drain
events produced by :mod:`app.jitter`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .config import JitterConfig
from .media import RtpPacket, RtpParseError, parse_rtp
from .sessions import SessionRegistry


@dataclass
class TraceEvent:
    """One wire packet as observed at the receiver."""
    seq: int
    timestamp: int
    ssrc: int
    arrival_ms: float
    payload: bytes = b""
    marker: bool = False
    raw: Optional[bytes] = None  # if set, parsed through the RTP parser


@dataclass
class IngestRecord:
    arrival_ms: float
    ssrc: int
    seq: int
    raw_seq: int
    raw_ts: int
    status: str
    detail: str = ""


@dataclass
class PlayoutRecord:
    index: int
    kind: str
    ssrc: int
    ext_seq: int
    ext_ts: int
    playout_ms: float
    sender_ms: float
    delay_ms: float


@dataclass
class SessionMetrics:
    ssrc: int
    ingested_accepted: int
    reordered: int
    duplicates: int
    late_after_playout: int
    buffer_full: int
    played_audio: int
    gaps: int
    max_buffer_size: int
    min_delay_ms: Optional[float]
    max_delay_ms: Optional[float]
    mean_delay_ms: Optional[float]


@dataclass
class TraceResult:
    mode: str
    config: Dict
    monotonic: bool
    monotonic_violations: int
    buffer_bounded: bool
    max_buffer_observed: int
    ingest: List[IngestRecord] = field(default_factory=list)
    playout: List[PlayoutRecord] = field(default_factory=list)
    sessions: List[SessionMetrics] = field(default_factory=list)
    parse_errors: List[Dict] = field(default_factory=list)
    uncertainty: List[str] = field(default_factory=list)
    totals: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        d = asdict(self)
        return d


def _run(events: List[TraceEvent], cfg: JitterConfig, adaptive: bool,
         mode: str) -> TraceResult:
    registry = SessionRegistry(cfg, adaptive=adaptive)
    result = TraceResult(
        mode=mode,
        config={
            "min_delay_ms": cfg.min_delay_ms,
            "max_delay_ms": cfg.max_delay_ms,
            "safety_margin_ms": cfg.safety_margin_ms,
            "jitter_multiplier": cfg.jitter_multiplier,
            "fixed_delay_ms": cfg.fixed_delay_ms,
            "clock_rate": cfg.clock_rate,
            "samples_per_packet": cfg.samples_per_packet,
            "max_buffer_packets": cfg.max_buffer_packets,
            "adaptive": adaptive,
        },
        monotonic=True,
        monotonic_violations=0,
        buffer_bounded=True,
        max_buffer_observed=0,
    )

    ordered = sorted(events, key=lambda e: e.arrival_ms)
    playout_index = 0
    last_by_ssrc: Dict[int, float] = {}

    def drain(now: float) -> None:
        nonlocal playout_index
        for item in registry.drain(now):
            result.playout.append(PlayoutRecord(
                index=playout_index, kind=item.kind.value, ssrc=item.ssrc,
                ext_seq=item.ext_seq, ext_ts=item.ext_ts,
                playout_ms=item.playout_ms, sender_ms=item.sender_ms,
                delay_ms=item.delay_ms))
            prev = last_by_ssrc.get(item.ssrc)
            if prev is not None and item.playout_ms + 1e-9 < prev:
                result.monotonic = False
                result.monotonic_violations += 1
            last_by_ssrc[item.ssrc] = item.playout_ms
            playout_index += 1

    for idx, ev in enumerate(ordered):
        if ev.raw is not None:
            try:
                pkt = parse_rtp(ev.raw, ev.arrival_ms)
            except RtpParseError as exc:
                result.parse_errors.append(
                    {"event_index": idx, "arrival_ms": ev.arrival_ms,
                     "reason": exc.reason, "detail": exc.detail})
                continue
        else:
            pkt = RtpPacket(
                version=2, padding=False, extension=False, marker=ev.marker,
                payload_type=0, seq=ev.seq & 0xFFFF,
                timestamp=ev.timestamp & 0xFFFFFFFF, ssrc=ev.ssrc & 0xFFFFFFFF,
                payload=ev.payload, arrival_ms=ev.arrival_ms)
        rec = registry.ingest(pkt)
        result.ingest.append(IngestRecord(
            arrival_ms=ev.arrival_ms, ssrc=ev.ssrc, seq=rec.ext_seq,
            raw_seq=ev.seq & 0xFFFF, raw_ts=ev.timestamp & 0xFFFFFFFF,
            status=rec.status.value, detail=rec.detail))
        result.max_buffer_observed = max(result.max_buffer_observed,
                                         rec.buffer_size)

        # drain at every distinct arrival time (online-style)
        nxt = ordered[idx + 1].arrival_ms if idx + 1 < len(ordered) else None
        if nxt is None or nxt > ev.arrival_ms:
            drain(ev.arrival_ms)

    registry.close_all()
    horizon = (max((e.arrival_ms for e in ordered), default=0.0)
               + cfg.max_delay_ms + cfg.frame_ms)
    drain(horizon)

    for s in registry.sessions:
        st = s.stats
        result.sessions.append(SessionMetrics(
            ssrc=st.ssrc, ingested_accepted=st.accepted,
            reordered=st.reordered, duplicates=st.duplicates,
            late_after_playout=st.late_after_playout,
            buffer_full=st.buffer_full, played_audio=st.played_audio,
            gaps=st.gaps, max_buffer_size=st.max_buffer_size,
            min_delay_ms=st.min_delay_used_ms,
            max_delay_ms=st.max_delay_used_ms,
            mean_delay_ms=(float(np.mean(st.delays_used_ms))
                           if st.delays_used_ms else None)))

    result.buffer_bounded = (
        result.max_buffer_observed <= cfg.max_buffer_packets)

    result.totals = {
        "accepted": sum(s.ingested_accepted for s in result.sessions),
        "reordered": sum(s.reordered for s in result.sessions),
        "duplicates": sum(s.duplicates for s in result.sessions),
        "late_after_playout": sum(s.late_after_playout for s in result.sessions),
        "buffer_full": sum(s.buffer_full for s in result.sessions),
        "played_audio": sum(s.played_audio for s in result.sessions),
        "gaps": sum(s.gaps for s in result.sessions),
        "parse_errors": len(result.parse_errors),
    }

    undrained = sum(registry.session(m.ssrc).buffered_count
                    for m in result.sessions)
    if undrained:
        result.uncertainty.append(
            f"{undrained} accepted packet(s) never reached a playout deadline")
    if result.parse_errors:
        result.uncertainty.append(
            f"{len(result.parse_errors)} packet(s) failed RTP parsing and "
            "were excluded from the session")
    return result


def run_comparison(events: List[TraceEvent],
                   cfg: JitterConfig) -> Dict[str, TraceResult]:
    """Run adaptive and fixed-delay baselines over the identical trace."""
    return {
        "adaptive": _run(events, cfg, adaptive=True, mode="adaptive"),
        "fixed": _run(events, cfg, adaptive=False, mode="fixed"),
    }
