"""Direct unit tests of the jitter-buffer mechanics and discard semantics."""
import numpy as np
import pytest

from app.config import JitterConfig
from app.jitter import (IngestStatus, JitterSession, PlayoutKind)
from app.media import RtpPacket
from tests.conftest import assert_exactly_monotonic


def make_packet(seq, ts, ssrc=1, arrival_ms=0.0, payload=b"\x80",
                marker=False):
    return RtpPacket(2, False, False, marker, 0, seq & 0xFFFF,
                     ts & 0xFFFFFFFF, ssrc, payload, arrival_ms)


def feed(session, rows, samples=80):
    """rows: (ext_seq, ext_ts, arrival_ms[, marker]) -> ingest result."""
    out = []
    for row in rows:
        seq, ts, arr = row[0], row[1], row[2]
        marker = row[3] if len(row) > 3 else False
        out.append(session.ingest(make_packet(seq, ts, session.ssrc, arr,
                                              marker=marker)))
    return out


# ------------------------------------------------------------- seq/timestamp
def test_sequence_numbers_extend_across_wrap(cfg):
    s = JitterSession(1, cfg)
    res = feed(s, [(65534, 0, 0), (65535, 80, 10), (0, 160, 20),
                   (1, 240, 30)])
    assert [r.ext_seq for r in res] == [0, 1, 2, 3]
    assert res[0].status == IngestStatus.ACCEPTED


def test_timestamp_extends_across_32bit_wrap(cfg):
    s = JitterSession(1, cfg)
    near = 0xFFFFFFFF - 40
    res = feed(s, [(0, near, 0), (1, near + 80, 10), (2, near + 160, 20)])
    assert res[1].ext_ts == near + 80
    assert res[2].ext_ts == near + 160  # > 2^32, unwrapped


def test_ssrc_change_means_independent_sessions(cfg):
    from app.sessions import SessionRegistry
    reg = SessionRegistry(cfg)
    r1 = reg.ingest(make_packet(5, 0, ssrc=0x111, arrival_ms=0))
    r2 = reg.ingest(make_packet(5, 0, ssrc=0x222, arrival_ms=1))
    assert r1.ext_seq == 0 and r2.ext_seq == 0   # same raw seq, new session
    assert set(reg.ssrcs) == {0x111, 0x222}


# ------------------------------------------------------------- discard rules
def test_duplicate_packet_classified(cfg):
    s = JitterSession(1, cfg)
    feed(s, [(0, 0, 0), (1, 80, 10)])
    dup = s.ingest(make_packet(0, 0, 1, arrival_ms=12))
    assert dup.status == IngestStatus.DUPLICATE
    assert s.stats.duplicates == 1


def test_late_packet_after_gap_cannot_reinsert(cfg):
    s = JitterSession(1, cfg)
    # play seq 0, then seq 1 goes missing past its deadline -> GAP
    feed(s, [(0, 0, 0)])
    items = s.drain(cfg.fixed_delay_ms + 20)
    s.close_stream(last_ext_seq=2)
    items = s.drain(cfg.fixed_delay_ms + 20 + cfg.frame_ms)
    kinds = [i.kind for i in items]
    assert PlayoutKind.GAP in kinds
    # now seq 1 finally arrives
    late = s.ingest(make_packet(1, 80, 1, arrival_ms=9999))
    assert late.status == IngestStatus.LATE_AFTER_PLAYOUT
    assert s.stats.late_after_playout == 1


def test_reordered_packet_within_deadline_is_saved(cfg):
    s = JitterSession(1, cfg)
    feed(s, [(0, 0, 0), (2, 160, 8), (1, 80, 9)])
    assert s.stats.reordered == 1
    items = s.drain(500)
    seqs = [i.ext_seq for i in items]
    assert seqs == [0, 1, 2]
    assert all(i.kind == PlayoutKind.AUDIO for i in items)


def test_buffer_capacity_is_bounded(cfg):
    tight = JitterConfig(max_buffer_packets=4)
    s = JitterSession(1, tight)
    results = feed(s, [(i, i * 80, i) for i in range(6)])
    assert results[4].status == IngestStatus.BUFFER_FULL
    assert results[5].status == IngestStatus.BUFFER_FULL
    assert s.buffered_count == 4


# ------------------------------------------------------------- gaps / audio
def test_gap_marker_has_no_audio_and_explicit_length(cfg):
    s = JitterSession(1, cfg)
    feed(s, [(0, 0, 0), (2, 160, 10)])   # seq 1 absent
    s.close_stream(last_ext_seq=2)
    items = s.drain(1000)
    gaps = [i for i in items if i.kind == PlayoutKind.GAP]
    assert len(gaps) == 1
    g = gaps[0]
    assert g.audio is None
    assert g.gap_length_samples == cfg.samples_per_packet
    # no real audio content is fabricated anywhere
    assert all(i.audio is not None for i in items
               if i.kind == PlayoutKind.AUDIO)


def test_playout_is_strictly_monotonic_under_bursty_reorder(cfg):
    rng = np.random.RandomState(0)
    s = JitterSession(1, cfg)
    rows = [(i, i * 80, i * cfg.frame_ms + rng.uniform(0, 45))
            for i in range(60)]
    for lo in (10, 30, 50):
        rows[lo:lo + 6] = list(reversed(rows[lo:lo + 6]))
    feed(s, rows)
    s.close_stream()
    items = s.drain(100000)
    records = [{"ssrc": i.ssrc, "ext_seq": i.ext_seq,
                "playout_ms": i.playout_ms} for i in items]
    assert_exactly_monotonic(records, cfg.frame_ms)
    # frame spacing exactly
    times = [i.playout_ms for i in items]
    deltas = [b - a for a, b in zip(times, times[1:])]
    assert deltas and all(d == pytest.approx(cfg.frame_ms) for d in deltas)


# ------------------------------------------------------------- adaptive delay
def test_adaptive_delay_respects_bounds(cfg):
    s = JitterSession(1, cfg, adaptive=True)
    rng = np.random.RandomState(2)
    for i in range(200):
        # large varying transit: jitter estimate must grow, then clip
        arr = i * cfg.frame_ms + rng.uniform(0, 80)
        s.ingest(make_packet(i, i * 80, arrival_ms=arr))
    s.close_stream()
    items = s.drain(100000)
    delays = {round(i.delay_ms, 9) for i in items}
    assert min(delays) >= cfg.min_delay_ms - 1e-9
    assert max(delays) <= cfg.max_delay_ms + 1e-9


def test_min_delay_floor_applied_on_clean_channel(cfg):
    s = JitterSession(1, cfg, adaptive=True)
    feed(s, [(i, i * 80, i * cfg.frame_ms) for i in range(50)])
    s.close_stream()
    items = s.drain(10000)
    delays = [i.delay_ms for i in items]
    # zero jitter -> formula gives the safety margin, floored to min_delay
    assert min(delays) == pytest.approx(cfg.min_delay_ms)


def test_fixed_mode_uses_constant_delay(cfg):
    s = JitterSession(1, cfg, adaptive=False)
    feed(s, [(i, i * 80, i * cfg.frame_ms + (30 if i % 5 else 0))
             for i in range(50)])
    s.close_stream()
    items = s.drain(10000)
    assert {round(i.delay_ms, 9) for i in items} == {cfg.fixed_delay_ms}


def test_pause_restart_restarts_talkspurt_without_clock_rewind(cfg):
    s = JitterSession(1, cfg, adaptive=True)
    rows = [(i, i * 80, i * cfg.frame_ms) for i in range(30)]
    pause_ticks = 200 * cfg.clock_rate // 1000
    for j in range(30):
        seq = 30 + j
        ts = 30 * 80 + pause_ticks + j * 80
        wall = 30 * cfg.frame_ms + 200 + j * cfg.frame_ms
        rows.append((seq, ts, wall, j == 0))
    feed(s, rows)
    s.close_stream()
    items = s.drain(100000)
    times = [i.playout_ms for i in items]
    # strictly monotonic and no playout lands before the last pre-pause item
    assert all(b > a for a, b in zip(times, times[1:]))
    assert times[-1] > 30 * cfg.frame_ms + 200
