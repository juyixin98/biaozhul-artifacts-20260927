"""Synthetic, locally generated RTP receive traces.

Each fixture is a *reference generator* independent of the jitter core: it
only knows sender ordering, a network disturbance model, and receiver
arrival times. The expected ground truth (which seqs exist, which were
duplicated, which were lost) is returned alongside the trace so tests can
assert concrete outcomes rather than merely "the API ran".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from app.config import JitterConfig
from app.engine import TraceEvent
from app.media import build_rtp
from app.time_kernel import DriftModel


def _payload(seq: int, samples: int) -> bytes:
    """Deterministic, seq-specific 8-bit PCM payload (128 = silence)."""
    rng = np.random.RandomState(1000 + seq)
    tone = 128 + (40 * np.sin(2 * np.pi * (seq % 7) / 7 *
                              np.arange(samples) / samples))
    noise = rng.randint(-3, 4, size=samples)
    return np.clip(tone + noise, 0, 255).astype(np.uint8).tobytes()


@dataclass
class FixtureSpec:
    name: str
    description: str
    events: List[TraceEvent]
    existing_seqs: List[int] = field(default_factory=list)   # distinct sent
    duplicated_seqs: List[int] = field(default_factory=list)
    lost_seqs: List[int] = field(default_factory=list)
    ssrc: int = 0xAABBCCDD
    first_seq: int = 0
    expected_packets: int = 0
    notes: List[str] = field(default_factory=list)


def _emit(name: str, description: str, arrivals: List[Tuple[int, int, float,
                                                            bool, bytes]],
          cfg: JitterConfig, ssrc: int, first_seq: int,
          total_seqs: int, duplicated: List[int], lost: List[int],
          existing: List[int], notes: List[str],
          wrap_raw: bool = True) -> FixtureSpec:
    events: List[TraceEvent] = []
    for seq, ts, arr, marker, payload in arrivals:
        raw_seq = seq & 0xFFFF if wrap_raw else seq
        raw_ts = ts & 0xFFFFFFFF
        raw = build_rtp(raw_seq, raw_ts, ssrc, payload, marker=marker)
        events.append(TraceEvent(seq=raw_seq, timestamp=raw_ts, ssrc=ssrc,
                                 arrival_ms=arr, payload=payload,
                                 marker=marker, raw=raw))
    events.sort(key=lambda e: e.arrival_ms)
    return FixtureSpec(name=name, description=description, events=events,
                       existing_seqs=sorted(existing),
                       duplicated_seqs=sorted(duplicated),
                       lost_seqs=sorted(lost), ssrc=ssrc, first_seq=first_seq,
                       expected_packets=total_seqs, notes=notes)


def ordered_trace(n_packets: int = 100, cfg: Optional[JitterConfig] = None,
                  start_ms: float = 0.0, jitter_ms: float = 0.0,
                  ssrc: int = 1, first_seq: int = 0, first_ts: int = 0,
                  rng: Optional[np.random.RandomState] = None) -> List[Tuple]:
    """Build clean (seq, ts, arrival_ms) tuples with optional zero-mean jitter."""
    cfg = cfg or JitterConfig()
    rng = rng or np.random.RandomState(42)
    drift = DriftModel(0.0, start_ms, 1.0)
    out = []
    for i in range(n_packets):
        seq = first_seq + i
        ts = first_ts + i * cfg.samples_per_packet
        sender_ms = 1000.0 * ts / cfg.clock_rate
        arr = drift.to_wall_ms(sender_ms) + (
            float(rng.uniform(-jitter_ms, jitter_ms)) if jitter_ms else 0.0)
        out.append((seq, ts, max(start_ms, arr), False,
                    _payload(seq, cfg.samples_per_packet)))
    return out


# --------------------------------------------------------------- fixtures
def burst_reorder_fixture(cfg: Optional[JitterConfig] = None) -> FixtureSpec:
    """Bursty reordering: whole blocks arrive out of order, some late."""
    cfg = cfg or JitterConfig()
    rng = np.random.RandomState(7)
    base = ordered_trace(120, cfg, jitter_ms=6.0, rng=rng)
    arrivals = list(base)

    lost = {40, 41, 42}                 # a 3-packet burst loss
    hole_before_late = {60, 61}         # packets just before the late pair
    duplicated = [10]
    # 62-63 are delayed until *after* packets 64..70 have arrived and the
    # 60-61 hole already played as a GAP -> LATE_AFTER_PLAYOUT. Their arrival
    # is pinned to an absolute late wall time, independent of sender index.
    late = {62: 900.0, 63: 900.0}

    # reorder two windows of 8 packets: reverse their order AND collapse
    # their arrival times into a burst so the receiver truly sees them
    # out of order (otherwise per-packet spacing would hide the reorder).
    for lo in (20, 80):
        window = arrivals[lo:lo + 8]
        burst_t = window[0][2]
        window = [(seq, ts, burst_t + k * 0.1, marker, payload)
                  for k, (seq, ts, _t, marker, payload)
                  in enumerate(reversed(window))]
        arrivals[lo:lo + 8] = window

    final = []
    for seq, ts, arr, marker, payload in arrivals:
        i = seq
        if i in lost or i in hole_before_late:
            continue
        if i in late:
            arr = late[i]  # absolute late wall-clock arrival
        final.append((seq, ts, arr, marker, payload))
        if i in duplicated:
            final.append((seq, ts, arr + 3.0, marker, payload))  # exact dup
    existing = sorted(set(seq for seq, *_ in final))
    all_lost = sorted(lost | hole_before_late)
    notes = [
        "seq windows [20..27] and [80..87] arrive reversed",
        "seq 40-42 lost; seq 60-61 lost and seq 62-63 delayed +320ms so "
        "they land after the gap deadline; seq 10 duplicated",
    ]
    return _emit("burst_reorder",
                 "Bursty reordering, burst losses, late-after-playout packets "
                 "and one duplicate",
                 final, cfg, ssrc=0xAABBCCDD, first_seq=0, total_seqs=120,
                 duplicated=duplicated, lost=all_lost, existing=existing,
                 notes=notes)


def drift_fixture(cfg: Optional[JitterConfig] = None, skew: float = 0.985
                  ) -> FixtureSpec:
    """Sender clock runs ~1.5% slow; network jitter moderate."""
    cfg = cfg or JitterConfig()
    rng = np.random.RandomState(11)
    n = 300
    drift = DriftModel(0.0, 0.0, skew)
    rows = []
    for i in range(n):
        seq = i
        ts = i * cfg.samples_per_packet
        sender_ms = 1000.0 * ts / cfg.clock_rate
        arr = drift.to_wall_ms(sender_ms) + float(rng.uniform(-4, 14))
        rows.append((seq, ts, max(0.0, arr), False,
                     _payload(seq, cfg.samples_per_packet)))
    return _emit("clock_drift",
                 f"Sender clock skew={skew} (slow) with 18ms network jitter",
                 rows, cfg, ssrc=0x11223344, first_seq=0, total_seqs=n,
                 duplicated=[], lost=[],
                 existing=list(range(n)),
                 notes=["transit time trends upward across the trace"])


def wraparound_fixture(cfg: Optional[JitterConfig] = None) -> FixtureSpec:
    """Stream that crosses both the 16-bit seq and 32-bit ts wrap boundaries."""
    cfg = cfg or JitterConfig()
    rng = np.random.RandomState(23)
    n = 140
    first_seq = (1 << 16) - 60      # wraps after 60 packets
    first_ts = (1 << 32) - 60 * cfg.samples_per_packet
    rows = []
    for k in range(n):
        ext_seq = first_seq + k
        ext_ts = first_ts + k * cfg.samples_per_packet
        sender_ms = 1000.0 * (k * cfg.samples_per_packet) / cfg.clock_rate
        arr = sender_ms + float(rng.uniform(0, 9))
        rows.append((ext_seq, ext_ts, arr, False,
                     _payload(ext_seq, cfg.samples_per_packet)))
    # duplicate one packet on each side of the seq wrap
    rows.append(rows[59][:])
    rows[-1] = (rows[-1][0], rows[-1][1], rows[-1][2] + 2.0,
                rows[-1][3], rows[-1][4])
    duplicated = [first_seq + 59]
    return _emit("wraparound",
                 "Crosses seq=65535->0 and ts=2^32->0 boundaries",
                 rows, cfg, ssrc=0x55667788, first_seq=first_seq & 0xFFFF,
                 total_seqs=n, duplicated=duplicated, lost=[],
                 existing=list(range(first_seq, first_seq + n)),
                 notes=["raw seq/timestamp values visibly wrap mid-trace"])


def pause_restart_fixture(cfg: Optional[JitterConfig] = None,
                          pause_ms: float = 2000.0) -> FixtureSpec:
    """Continuous seq numbers but a 2 s timestamp gap = pause/restart."""
    cfg = cfg or JitterConfig()
    rng = np.random.RandomState(31)
    n1, n2 = 60, 60
    rows = []
    for i in range(n1):
        ts = i * cfg.samples_per_packet
        sender_ms = 1000.0 * ts / cfg.clock_rate
        rows.append((i, ts, sender_ms + float(rng.uniform(0, 6)), False,
                     _payload(i, cfg.samples_per_packet)))
    gap_ticks = int(pause_ms * cfg.clock_rate / 1000)
    for j in range(n2):
        seq = n1 + j
        ts = (n1 * cfg.samples_per_packet) + gap_ticks + j * cfg.samples_per_packet
        wall = n1 * cfg.frame_ms + pause_ms + j * cfg.frame_ms
        rows.append((seq, ts, wall + float(rng.uniform(0, 6)), j == 0,
                     _payload(seq, cfg.samples_per_packet)))
    total = n1 + n2
    return _emit("pause_restart",
                 f"{pause_ms:.0f} ms sender pause: seq continuous, ts jumps, "
                 "marker on resume",
                 rows, cfg, ssrc=0x99AABBCC, first_seq=0, total_seqs=total,
                 duplicated=[], lost=[], existing=list(range(total)),
                 notes=["a fresh talkspurt starts at resume; playout delay "
                        "must not collapse across the silence"])


def ssrc_switch_fixture(cfg: Optional[JitterConfig] = None) -> FixtureSpec:
    """SSRC collision/collision-change mid-stream: two independent sessions."""
    cfg = cfg or JitterConfig()
    rng = np.random.RandomState(5)
    rows = ordered_trace(40, cfg, ssrc=0x11111111, rng=rng)
    s1, s2 = 0x11111111, 0x22222222
    rebuilt = []
    for k, (seq, ts, arr, marker, payload) in enumerate(rows):
        ssrc = s1 if k < 20 else s2
        # second SSRC restarts its own counters at 0
        seq2 = seq if k < 20 else seq - 20
        ts2 = ts if k < 20 else (seq - 20) * cfg.samples_per_packet
        rebuilt.append((seq2, ts2, arr, marker, payload, ssrc))
    events: List[TraceEvent] = []
    existing = []
    for seq, ts, arr, marker, payload, ssrc in rebuilt:
        raw = build_rtp(seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc, payload,
                        marker=marker)
        events.append(TraceEvent(seq=seq & 0xFFFF, timestamp=ts & 0xFFFFFFFF,
                                 ssrc=ssrc, arrival_ms=arr, payload=payload,
                                 marker=marker, raw=raw))
        existing.append((ssrc, seq))
    events.sort(key=lambda e: e.arrival_ms)
    spec = FixtureSpec(
        name="ssrc_switch",
        description="SSRC changes at packet 20; counters restart independently",
        events=events, existing_seqs=[], duplicated_seqs=[], lost_seqs=[],
        ssrc=s1, first_seq=0, expected_packets=40,
        notes=["two sessions (SSRCs) must be created, seq 0..19 not "
               "duplicated by the second source"])
    spec.ssrcs = [s1, s2]  # type: ignore[attr-defined]
    return spec


def malformed_fixture(cfg: Optional[JitterConfig] = None) -> FixtureSpec:
    """Good trace with injected truncated / bad-version / bad-padding datagrams."""
    cfg = cfg or JitterConfig()
    spec = ordered_trace(40, cfg)
    good = _emit("malformed", "Valid packets interleaved with malformed ones",
                 [], cfg, ssrc=0xDEADBEEF, first_seq=0, total_seqs=40,
                 duplicated=[], lost=[], existing=list(range(40)),
                 notes=["3 malformed datagrams must be reported, not crash"])
    good_pkts = {seq: (ts, arr, payload) for seq, ts, arr, _, payload in spec}
    bad: Dict[int, bytes] = {
        10: b"\x80",                                   # truncated header
        20: bytes([0x20]) + b"\x00" * 11 + b"\x01",    # bad version
        30: (bytes([0xA0, 0x00]) + (0).to_bytes(2, "big")
             + (0).to_bytes(4, "big") + (0).to_bytes(4, "big")
             + b"\x00\x05"),                           # padding length 5 > payload
    }
    events: List[TraceEvent] = []
    for seq in range(40):
        ts, arr, payload = good_pkts[seq]
        if seq in bad:
            events.append(TraceEvent(seq=seq & 0xFFFF, timestamp=ts & 0xFFFFFFFF,
                                     ssrc=0xDEADBEEF, arrival_ms=arr,
                                     raw=bad[seq]))
        else:
            raw = build_rtp(seq & 0xFFFF, ts & 0xFFFFFFFF, 0xDEADBEEF, payload)
            events.append(TraceEvent(seq=seq & 0xFFFF, timestamp=ts & 0xFFFFFFFF,
                                     ssrc=0xDEADBEEF, arrival_ms=arr,
                                     payload=payload, raw=raw))
    good.events = sorted(events, key=lambda e: e.arrival_ms)
    good.existing_seqs = [s for s in range(40) if s not in bad]
    return good


ALL_FIXTURES = ["burst_reorder", "ramp_jitter", "clock_drift", "wraparound",
                "pause_restart", "ssrc_switch", "malformed"]


def ramp_jitter_fixture(cfg: Optional[JitterConfig] = None) -> FixtureSpec:
    """Network jitter ramps from ~0 to ~70ms, then recovers.

    A fixed 20ms playout must declare gaps/late packets during the high-jitter
    middle section; the adaptive estimator grows its window in advance and
    rescues packets that the fixed baseline loses.
    """
    cfg = cfg or JitterConfig()
    rng = np.random.RandomState(99)
    n = 240
    rows = []
    for i in range(n):
        phase = i / n
        # triangle envelope: 0 -> 70ms at midpoint -> 0
        env = 70.0 * (1 - abs(2 * phase - 1))
        one_way = env * (0.4 + 0.6 * rng.random())
        seq, ts = i, i * cfg.samples_per_packet
        sender_ms = 1000.0 * ts / cfg.clock_rate
        rows.append((seq, ts, sender_ms + one_way, False,
                     _payload(seq, cfg.samples_per_packet)))
    return _emit("ramp_jitter",
                 "One-way jitter ramps 0->70->0 ms; adaptive vs fixed baseline",
                 rows, cfg, ssrc=0x0BADF00D, first_seq=0, total_seqs=n,
                 duplicated=[], lost=[], existing=list(range(n)),
                 notes=["fixed 20ms baseline should incur more gaps/late "
                        "discards than the adaptive window during the peak"])


def build(name: str, cfg: Optional[JitterConfig] = None) -> FixtureSpec:
    table = {
        "burst_reorder": burst_reorder_fixture,
        "ramp_jitter": ramp_jitter_fixture,
        "clock_drift": drift_fixture,
        "wraparound": wraparound_fixture,
        "pause_restart": pause_restart_fixture,
        "ssrc_switch": ssrc_switch_fixture,
        "malformed": malformed_fixture,
    }
    if name not in table:
        raise KeyError(f"unknown fixture {name!r}; choose from {ALL_FIXTURES}")
    return table[name](cfg)
