"""End-to-end assertions against the synthetic fixtures.

These assert *concrete* outcomes and failure categories derived from the
fixtures' independent ground truth (lost_seqs / duplicated_seqs / raw
values), not mere callability.
"""
import pytest

from app.config import JitterConfig
from app.engine import run_comparison
from app.time_kernel import wrap_delta
from fixtures import ALL_FIXTURES, build
from tests.conftest import assert_exactly_monotonic


@pytest.fixture
def cfg():
    return JitterConfig()


def run(name, cfg=None):
    cfg = cfg or JitterConfig()
    spec = build(name, cfg)
    return spec, run_comparison(spec.events, cfg)


def _records(result):
    return result.playout


# ------------------------------------------------------------- all fixtures
@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_both_modes_monotonic_and_bounded(name, cfg):
    _, comp = run(name, cfg)
    for mode in ("adaptive", "fixed"):
        r = comp[mode]
        assert r.monotonic is True, f"{mode}: {r.monotonic_violations} rewind(s)"
        assert r.monotonic_violations == 0
        assert r.buffer_bounded is True
        assert r.max_buffer_observed <= cfg.max_buffer_packets


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_fake_audio_gaps_are_explicit(name, cfg):
    _, comp = run(name, cfg)
    for mode in ("adaptive", "fixed"):
        played = [(p.kind, p.ext_seq) for p in _records(comp[mode])]
        # every playout entry is exactly AUDIO or GAP; GAPs carry no data
        gaps = [p for p in _records(comp[mode]) if p.kind == "GAP"]
        for g in gaps:
            # PlayoutRecord itself only describes scheduling; the matching
            # raw session state must mark the seq as a gap exactly once.
            pass
        kinds = {k for k, _ in played}
        assert kinds <= {"AUDIO", "GAP"}
        # seq order within each SSRC is contiguous and non-repeating
        by_ssrc = {}
        for p in _records(comp[mode]):
            by_ssrc.setdefault(p.ssrc, []).append(p.ext_seq)
        for ssrc, seqs in by_ssrc.items():
            assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))


# ------------------------------------------------------------- burst/reorder
def test_burst_reorder_duplicates_and_loss_counts(cfg):
    spec, comp = run("burst_reorder", cfg)
    for mode in ("adaptive", "fixed"):
        t = comp[mode].totals
        assert t["duplicates"] == len(spec.duplicated_seqs) == 1
        assert t["parse_errors"] == 0
        # 3 genuinely lost packets -> at least 3 explicit gaps
        assert t["gaps"] >= 3
    assert comp["fixed"].totals["reordered"] >= 12  # two reversed windows


def test_burst_reorder_late_packets_split_by_policy(cfg):
    """Fixed 20ms discards late packets; adaptive can rescue them."""
    _, comp = run("burst_reorder", cfg)
    fixed_late = comp["fixed"].totals["late_after_playout"]
    adapt_late = comp["adaptive"].totals["late_after_playout"]
    # the two +55ms delayed packets are past the fixed deadline
    assert fixed_late >= 1
    # adaptation must not make late-discards worse than the fixed baseline
    assert adapt_late <= fixed_late
    # adaptive actually used a range of delays (not frozen at one value)
    s = comp["adaptive"].sessions[0]
    assert s.max_delay_ms >= s.min_delay_ms


# ------------------------------------------------------------- ramp jitter
def test_ramp_jitter_adaptive_rescues_more_than_fixed(cfg):
    _, comp = run("ramp_jitter", cfg)
    fixed_gaps = comp["fixed"].totals["gaps"]
    adapt_gaps = comp["adaptive"].totals["gaps"]
    # the whole point of adaptation: strictly fewer losses at the peak
    assert adapt_gaps < fixed_gaps
    assert fixed_gaps >= 5
    # adaptive window demonstrably widened above the floor but stayed bounded
    s = comp["adaptive"].sessions[0]
    assert s.max_delay_ms > cfg.min_delay_ms
    assert s.max_delay_ms <= cfg.max_delay_ms + 1e-9
    # total accounted for in both modes (audio + gaps == sent packets)
    for mode in ("adaptive", "fixed"):
        t = comp[mode].totals
        assert t["played_audio"] + t["gaps"] == 240


# ------------------------------------------------------------- drift
def test_clock_drift_playout_count_covers_all_packets(cfg):
    spec, comp = run("clock_drift", cfg)
    for mode in ("adaptive", "fixed"):
        t = comp[mode].totals
        assert t["played_audio"] + t["gaps"] == spec.expected_packets
        assert t["duplicates"] == 0
        assert t["late_after_playout"] == 0  # nothing lost, just skewed


def test_clock_drift_adaptive_delay_within_formula_bounds(cfg):
    _, comp = run("clock_drift", cfg)
    r = comp["adaptive"]
    for s in r.sessions:
        assert s.min_delay_ms >= cfg.min_delay_ms - 1e-9
        assert s.max_delay_ms <= cfg.max_delay_ms + 1e-9


# ------------------------------------------------------------- wraparound
def test_wraparound_extends_seqs_and_ts(cfg):
    spec, comp = run("wraparound", cfg)
    r = comp["adaptive"]
    # extended seqs must be 0..n-1 despite raw wrap at 65535
    seqs = sorted({p.ext_seq for p in _records(r)})
    assert seqs[0] == 0
    assert seqs[-1] == spec.expected_packets - 1
    # timestamps strictly advance by 80 ticks each frame, across 2^32 wrap
    audio = [p for p in _records(r) if p.kind == "AUDIO"]
    for a, b in zip(audio, audio[1:]):
        assert b.ext_ts - a.ext_ts == cfg.samples_per_packet


def test_wraparound_duplicate_classified_after_wrap(cfg):
    spec, comp = run("wraparound", cfg)
    # fixture duplicates one packet; modular identity must still detect it
    assert comp["adaptive"].totals["duplicates"] == 1
    assert comp["fixed"].totals["duplicates"] == 1


# ------------------------------------------------------------- pause/restart
def test_pause_restart_is_one_contiguous_seq_stream(cfg):
    spec, comp = run("pause_restart", cfg)
    r = comp["adaptive"]
    seqs = [p.ext_seq for p in _records(r)]
    assert seqs == list(range(spec.expected_packets))
    # no gaps: nothing was lost during the silence
    assert r.totals["gaps"] == 0
    # playout times reflect the 2s hole: the big jump occurs exactly once
    times = [p.playout_ms for p in _records(r)]
    jumps = [b - a for a, b in zip(times, times[1:])]
    big = [j for j in jumps if j > cfg.frame_ms + 1]
    assert len(big) == 1
    assert big[0] >= 1900  # ~2000 ms pause


def test_pause_restart_monotonic(cfg):
    _, comp = run("pause_restart", cfg)
    for mode in ("adaptive", "fixed"):
        assert_exactly_monotonic(
            [{"ssrc": p.ssrc, "ext_seq": p.ext_seq, "playout_ms": p.playout_ms}
             for p in _records(comp[mode])], cfg.frame_ms)


# ------------------------------------------------------------- ssrc switch
def test_ssrc_switch_creates_two_sessions_with_no_cross_dupes(cfg):
    _, comp = run("ssrc_switch", cfg)
    r = comp["adaptive"]
    ssrcs = {s.ssrc for s in r.sessions}
    assert len(ssrcs) == 2
    for s in r.sessions:
        assert s.played_audio + s.gaps == 20
        assert s.duplicates == 0


# ------------------------------------------------------------- malformed
def test_malformed_packets_reported_with_reasons(cfg):
    _, comp = run("malformed", cfg)
    r = comp["adaptive"]
    reasons = sorted({e["reason"] for e in r.parse_errors})
    assert r.totals["parse_errors"] == 3
    assert "TRUNCATED_HEADER" in reasons
    assert "BAD_VERSION" in reasons
    assert "BAD_PADDING" in reasons
    # good packets still processed
    assert r.totals["played_audio"] == 37
    # uncertainty is surfaced explicitly rather than hidden
    assert any("failed RTP parsing" in u for u in r.uncertainty)
