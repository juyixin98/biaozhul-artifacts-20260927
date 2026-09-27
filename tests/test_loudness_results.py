"""Concrete loudness/LRA result tests against independent references.

Each test asserts specific numbers and specific status/failure categories -
not just that an endpoint or function returns. The gated targets come from the
independent ffmpeg ebur128 C implementation (see tests/reference.py); the
pyloudnorm third-party meter is a second, code-level independent oracle for
integrated loudness.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.loudness import analyze_array
from app.r128_constants import (
    ABSOLUTE_GATE_LUFS,
    CHANNEL_WEIGHT_SURROUND,
)
from tests import fixtures as fx
from tests.fixtures import FS

# Tight tolerance vs ffmpeg: 0.1 LU rounding (ffmpeg prints one decimal) plus
# float/quantisation headroom.
FFMPEG_TOL_LU = 0.12
PYLN_TOL_LU = 0.15


# ---------------------------------------------------------------------------
# Silence: dedicated status, never a fabricated -70 number
# ---------------------------------------------------------------------------
def test_digital_silence_is_dedicated_status_not_number():
    r = analyze_array(fx.digital_silence(3.0))
    assert r.status == "SILENCE"
    assert r.integrated_loudness_lufs is None
    assert r.lra.status == "SILENCE"
    assert r.lra.loudness_range_lu is None
    assert r.signal.sample_peak == 0.0
    assert r.gating.blocks_total > 0
    assert r.gating.blocks_above_absolute == 0


def test_very_quiet_signal_below_gate_is_not_computed_not_silence():
    # Non-zero samples, but every block below -70 LUFS.
    x = fx.quiet_below_abs_gate(2.0, lufs=-85.0)
    r = analyze_array(x)
    assert r.status == "NOT_COMPUTED"
    assert r.integrated_loudness_lufs is None
    assert r.signal.sample_peak > 0  # it was real signal, not silence
    assert r.gating.blocks_total > 0
    assert r.gating.blocks_above_absolute == 0


# ---------------------------------------------------------------------------
# Constant / calibrated signal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("channels", [1, 2])
def test_calibrated_tone_matches_ffmpeg(channels, ffmpeg):
    if not ffmpeg.available:
        pytest.skip("ffmpeg not available on this machine")
    x = fx.calibrated_loudness_tone(5.0, -23.0, channels=channels)
    r = analyze_array(x)
    ref = ffmpeg.measure(x)
    expected = -23.0 if channels == 1 else -20.0
    assert r.status == "OK"
    assert r.integrated_loudness_lufs == pytest.approx(expected, abs=0.05)
    assert ref.integrated_lufs == pytest.approx(expected, abs=0.05)
    assert r.integrated_loudness_lufs == pytest.approx(
        ref.integrated_lufs, abs=FFMPEG_TOL_LU)
    # relative gate must sit exactly 10 LU below the gated loudness
    assert r.gating.relative_gate_lufs == pytest.approx(expected - 10.0,
                                                        abs=0.05)
    assert r.gating.blocks_total == r.gating.blocks_above_absolute == \
        r.gating.blocks_above_relative
    # all blocks identical -> LRA 0 and identical percentiles
    assert r.lra.status == "OK"
    assert r.lra.loudness_range_lu == 0.0
    assert r.lra.percentile_10_lufs == r.lra.percentile_95_lufs


def test_calibrated_tone_matches_pyloudnorm_independent(pyln_ref):
    x = fx.calibrated_loudness_tone(5.0, -23.0, channels=2)
    ours = analyze_array(x).integrated_loudness_lufs
    theirs = pyln_ref.integrated_loudness(x)
    assert ours == pytest.approx(theirs, abs=PYLN_TOL_LU)


# ---------------------------------------------------------------------------
# Short bursts: exact boundary status categories
# ---------------------------------------------------------------------------
def test_burst_longer_than_400ms_is_measured(ffmpeg):
    # 250 ms burst inside 2 s: six complete 400 ms windows straddle it.
    x = fx.tone_burst(2.0, 0.5, 0.25, lufs=-23.0)
    r = analyze_array(x)
    assert r.status == "OK"
    assert r.gating.blocks_total == 17
    assert r.gating.blocks_above_absolute == 6
    assert r.integrated_loudness_lufs == pytest.approx(-26.8, abs=0.1)
    # only 2 s total -> no complete 3 s LRA block
    assert r.lra.status == "INSUFFICIENT_BLOCKS"
    assert r.lra.blocks_total == 0
    if ffmpeg.available:
        ref = ffmpeg.measure(x)
        assert r.integrated_loudness_lufs == pytest.approx(
            ref.integrated_lufs, abs=FFMPEG_TOL_LU)


def test_short_burst_exact_boundary_categories(ffmpeg):
    # A 200 ms burst in 2 s: four 400 ms windows straddle enough of it to pass
    # the absolute gate; gated integrated = -26.8 LUFS (matches ffmpeg).
    x = fx.tone_burst(2.0, 0.10, 0.20, lufs=-23.0)
    r = analyze_array(x)
    assert r.status == "OK"
    assert r.gating.blocks_total == 17
    assert r.gating.blocks_above_absolute == 4
    assert r.gating.blocks_above_relative == 3
    assert r.integrated_loudness_lufs == pytest.approx(-26.8, abs=0.1)
    assert r.lra.status == "INSUFFICIENT_BLOCKS"
    if ffmpeg.available:
        ref = ffmpeg.measure(x)
        assert r.integrated_loudness_lufs == pytest.approx(
            ref.integrated_lufs, abs=FFMPEG_TOL_LU)


@pytest.mark.parametrize("dur,expected", [(0.30, 0), (0.39, 0), (0.40, 1),
                                          (0.50, 2)])
def test_complete_block_count_vs_duration(dur, expected):
    # No complete 400 ms block until duration reaches 400 ms.
    r = analyze_array(fx.calibrated_loudness_tone(dur, -23.0))
    assert r.gating.blocks_total == expected
    if expected == 0:
        assert r.status == "INSUFFICIENT_BLOCKS"
        assert r.integrated_loudness_lufs is None


def test_kweighting_makes_equal_rms_differ_in_loudness(ffmpeg):
    """Anti-RMS test: a 40 Hz tone and a 1 kHz tone at identical RMS MUST have
    different gated loudness (the RLB high-pass removes low-frequency energy).
    A fake 'loudness = RMS + offset' implementation would return equal values.
    """
    dur, rms = 3.0, 0.07079  # equal RMS for both
    n = int(dur * FS)
    t = np.arange(n) / FS
    low = rms * np.sqrt(2) * np.sin(2 * np.pi * 40 * t)[:, None]
    high = rms * np.sqrt(2) * np.sin(2 * np.pi * 1000 * t)[:, None]
    assert np.sqrt(np.mean(low ** 2)) == pytest.approx(np.sqrt(np.mean(high**2)),
                                                       rel=1e-9)
    l_low = analyze_array(low).integrated_loudness_lufs
    l_high = analyze_array(high).integrated_loudness_lufs
    assert l_low is not None and l_high is not None
    assert (l_high - l_low) > 6.0  # K-weighting discriminates strongly
    if ffmpeg.available:
        assert l_high == pytest.approx(
            ffmpeg.measure(high).integrated_lufs, abs=FFMPEG_TOL_LU)


def test_lra_requires_three_seconds_but_integrated_needs_only_400ms():
    # 1 s of tone: integrated valid, LRA must report insufficient blocks.
    x = fx.calibrated_loudness_tone(1.0, -23.0)
    r = analyze_array(x)
    assert r.status == "OK"
    assert r.integrated_loudness_lufs == pytest.approx(-23.0, abs=0.1)
    assert r.lra.status == "INSUFFICIENT_BLOCKS"
    assert r.lra.loudness_range_lu is None


# ---------------------------------------------------------------------------
# Channel changes and weights
# ---------------------------------------------------------------------------
def test_left_only_then_right_only_stereo():
    r = analyze_array(fx.channel_switch(8.0))
    assert r.status == "OK"
    # each half is a full-scale -23 bed on one channel only
    assert r.integrated_loudness_lufs is not None
    assert -25.0 < r.integrated_loudness_lufs < -22.0


def test_surround_weight_adds_1p5db(ffmpeg):
    x = fx.surround_mix(4.0, -23.0)
    r = analyze_array(x)
    assert CHANNEL_WEIGHT_SURROUND == 1.41
    # weights in result: L,R,C =1, LFE =0, Ls,Rs =1.41
    assert r.channel_weights == [1.0, 1.0, 1.0, 0.0, 1.41, 1.41]
    assert r.status == "OK"
    if ffmpeg.available:
        ref = ffmpeg.measure(x)
        assert r.integrated_loudness_lufs == pytest.approx(
            ref.integrated_lufs, abs=FFMPEG_TOL_LU)
        assert r.lra.loudness_range_lu == pytest.approx(
            ref.lra_lu, abs=0.15)


def test_lfe_channel_does_not_change_loudness():
    # 5.0 layout (L R C Ls Rs, no LFE) vs the same beds in 5.1 with a LOUD LFE:
    # the LFE content must contribute zero to BS.1770 gated loudness.
    base = fx.calibrated_loudness_tone(4.0, -23.0)[:, 0]
    five = np.stack([base, base, base, base, base], axis=1)
    six = np.stack([
        base, base, base,
        0.9 * np.sin(2 * np.pi * 60 * np.arange(len(base)) / FS),  # loud LFE
        base, base,
    ], axis=1)
    r5 = analyze_array(
        five, roles=["L", "R", "C", "Ls", "Rs"]).integrated_loudness_lufs
    r6 = analyze_array(six).integrated_loudness_lufs
    assert r6 == pytest.approx(r5, abs=1e-9)


def test_dual_mono_role_is_3db_above_mono():
    mono = fx.calibrated_loudness_tone(4.0, -23.0)
    r_mono = analyze_array(mono).integrated_loudness_lufs
    r_dual = analyze_array(mono, roles=["DualMono"]).integrated_loudness_lufs
    assert r_dual - r_mono == pytest.approx(3.0103, abs=0.01)


# ---------------------------------------------------------------------------
# Multi-level LRA programme against ffmpeg
# ---------------------------------------------------------------------------
def test_multilevel_programme_lra_and_gates(ffmpeg):
    x = fx.segmented_programme(
        [(6, -36), (6, -18), (6, -30), (6, -24)], gap_seconds=2)
    r = analyze_array(x)
    assert r.status == "OK"
    # Reference targets below come from the independent ffmpeg ebur128 summary
    # on this exact deterministic fixture (see scripts/compare_reference.py):
    # I=-20.4, LRA 18.0 LU, LRA low -34.7 / high -16.7.
    assert r.integrated_loudness_lufs == pytest.approx(-20.4, abs=0.15)
    # The relative gate is (mean of absolute-gated blocks) - 10 LU; the final
    # gated integrated loudness is then re-averaged above that gate, so it is
    # NOT simply gate + 10. ffmpeg reports the gate itself as -31.6.
    assert r.gating.relative_gate_lufs == pytest.approx(-31.6, abs=0.2)
    assert r.gating.blocks_total == 317
    assert r.gating.blocks_above_absolute == 252
    assert r.gating.blocks_above_relative == 186
    assert r.lra.loudness_range_lu == pytest.approx(18.0, abs=0.15)
    # relative LRA gate = mean abs-gated 3s energy - 20 LU
    assert r.lra.relative_gate_lufs == pytest.approx(-42.4, abs=0.2)
    assert r.lra.percentile_10_lufs == pytest.approx(-34.7, abs=0.2)
    assert r.lra.percentile_95_lufs == pytest.approx(-16.7, abs=0.2)
    assert r.lra.blocks_total == 30
    assert r.lra.blocks_above_absolute == 30
    if ffmpeg.available:
        ref = ffmpeg.measure(x)
        assert not ref.is_silence_sentinel
        assert r.integrated_loudness_lufs == pytest.approx(
            ref.integrated_lufs, abs=FFMPEG_TOL_LU)
        assert r.lra.loudness_range_lu == pytest.approx(ref.lra_lu, abs=0.15)
        assert r.lra.percentile_10_lufs == pytest.approx(ref.lra_low_lufs,
                                                         abs=0.15)
        assert r.lra.percentile_95_lufs == pytest.approx(ref.lra_high_lufs,
                                                         abs=0.15)


def test_true_peak_is_explicitly_not_measured():
    r = analyze_array(fx.calibrated_loudness_tone(1.0, -23.0))
    assert r.signal.true_peak == "NOT_MEASURED"
    assert any("true-peak" in u for u in r.uncertainties)


def test_absolute_gate_constant_is_minus_70():
    r = analyze_array(fx.digital_silence(1.0))
    assert r.gating.absolute_gate_lufs == ABSOLUTE_GATE_LUFS


def test_long_quiet_signal_lra_not_computed_with_real_blocks():
    # 4 s of -75 dBFS noise: complete 400 ms AND 3 s blocks exist (non-silent)
    # but none clears -70 LUFS -> both integrated and LRA are NOT_COMPUTED
    # (distinct from INSUFFICIENT_BLOCKS, which means no complete window).
    x = fx.quiet_below_abs_gate(4.0, lufs=-78.0)
    r = analyze_array(x)
    assert r.gating.blocks_total > 0
    assert r.lra.blocks_total == 2  # 3 s windows ending at 3.0 s and 4.0 s
    assert r.status == "NOT_COMPUTED"
    assert r.lra.status == "NOT_COMPUTED"
    assert r.lra.blocks_above_absolute == 0
    assert r.integrated_loudness_lufs is None
    assert r.lra.loudness_range_lu is None
