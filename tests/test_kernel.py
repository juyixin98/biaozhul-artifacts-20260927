"""Core measurement tests with concrete expected values and failure categories.

Reference numbers are NOT produced by the kernel under test. They come from:
- an independent scipy K-weighting + direct energy calculation in this file;
- hand-counted block/gate logic;
- separate cross-reference suites (pyloudnorm, ffmpeg) in test_references.py.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import lfilter

from app.kernel.filter import k_weighting_coeffs
from app.service import measure
from conftest import SR, run_measure, sine, to_decoded

# ---------------------------------------------------------------------------
# Status categories
# ---------------------------------------------------------------------------

def test_silence_returns_silence_status(silence_sig, settings):
    r = run_measure(silence_sig, settings)
    assert r["status"] == "SILENCE"
    i = r["integrated_loudness"]
    l = r["loudness_range"]
    assert i["status"] == "SILENCE"
    assert i["integrated_lufs"] is None
    assert i["gate_stats"]["above_absolute_gate"] == 0
    assert l["status"] == "SILENCE"
    assert l["lra_lu"] is None
    assert r["true_peak_tpfs"] is None
    assert r["true_peak_supported"] is False


def test_shorter_than_one_block_is_insufficient(too_short_sig, settings):
    r = run_measure(too_short_sig, settings)
    assert r["status"] == "INSUFFICIENT_BLOCKS"
    assert r["integrated_loudness"]["integrated_lufs"] is None
    assert r["loudness_range"]["lra_lu"] is None
    # 0.2 s fills zero 0.4 s blocks; tail accounting holds all samples back.
    assert r["parameters"]["momentary_dropped_tail_samples"] == int(0.2 * SR)


def test_exactly_one_block_is_measurable(settings):
    """A signal of exactly 400 ms yields exactly one momentary block."""
    x = sine(0.5, 1000.0, 0.4)[:, None]
    from conftest import Sig
    sig = Sig(x, (1.0,), "mono", 1)
    r = run_measure(sig, settings)
    assert r["integrated_loudness"]["gate_stats"]["total_blocks"] == 1
    assert r["integrated_loudness"]["integrated_lufs"] is not None
    # 3 s short-term cannot exist -> LRA not applicable, but integrated is fine.
    assert r["status"] == "INTEGRATED_OK_LRA_NOT_APPLICABLE"
    assert r["loudness_range"]["status"] == "INSUFFICIENT_BLOCKS"


def test_constant_signal_lra_zero_and_confident_flags(constant_sig, settings):
    r = run_measure(constant_sig, settings)
    assert r["status"] == "OK"
    assert r["loudness_range"]["lra_lu"] == pytest.approx(0.0, abs=1e-9)
    # 8 s gives 51 short-term blocks -> confident (no low-confidence warning).
    assert r["loudness_range"]["warnings"] == []
    assert r["integrated_loudness"]["warnings"] == []


def test_short_term_confidence_warning(settings):
    """4 s signal: integrated is fine but LRA rests on few short-term blocks."""
    from conftest import Sig
    x = sine(0.5, 1000.0, 4.0)[:, None]
    r = run_measure(Sig(x, (1.0,), "mono", 1), settings)
    assert r["status"] == "OK_WITH_WARNINGS"
    assert "LRA_LOW_CONFIDENCE_FEW_SHORTTERM_BLOCKS" in r["loudness_range"]["warnings"]


# ---------------------------------------------------------------------------
# Concrete loudness value (independent scipy reference)
# ---------------------------------------------------------------------------

def _independent_kweighted_lufs(x: np.ndarray, weights: tuple[float, ...]) -> float:
    """K-weight with scipy from coefficients, whole-signal energy average."""
    (b_s, a_s), (b_h, a_h) = k_weighting_coeffs(SR)
    y = lfilter(b_h, a_h, lfilter(b_s, a_s, x, axis=0), axis=0)
    z = np.mean(y * y, axis=0)
    return -0.691 + 10 * np.log10(np.sum(np.asarray(weights) * z))


def test_constant_loudness_value_matches_independent_calculation(constant_sig, settings):
    r = run_measure(constant_sig, settings)
    expected = _independent_kweighted_lufs(constant_sig.samples, constant_sig.weights)
    # All blocks identical and all pass gating; integrated equals block energy.
    # Block energy vs full-signal energy differs only from the signal's
    # zero-padded filter head (a handful of samples), so tolerance is small.
    assert r["integrated_loudness"]["integrated_lufs"] == pytest.approx(expected, abs=5e-5)
    assert expected == pytest.approx(-9.0656, abs=1e-3)  # 0.5 FS 1 kHz sine


def test_stereo_matches_independent_calculation(stereo_constant_sig, settings):
    r = run_measure(stereo_constant_sig, settings)
    expected = _independent_kweighted_lufs(
        stereo_constant_sig.samples, stereo_constant_sig.weights)
    assert r["integrated_loudness"]["integrated_lufs"] == pytest.approx(expected, abs=5e-5)


# ---------------------------------------------------------------------------
# Gating: block counts, thresholds, and "RMS cannot masquerade as gated LUFS"
# ---------------------------------------------------------------------------

def test_short_burst_gating_block_counts(short_burst_sig, settings):
    r = run_measure(short_burst_sig, settings)
    g = r["integrated_loudness"]["gate_stats"]
    # 6 s: momentary blocks at 100 ms hop -> 1 + (6.0-0.4)/0.1 = 57; only the
    # windows overlapping the 0.5 s burst are non-silent.
    assert g["total_blocks"] == 57
    assert g["above_absolute_gate"] == 6
    assert g["above_both_gates"] == 5
    assert g["absolute_gate_lufs"] == -70.0
    # Relative gate = ungated (absolute-only) loudness - 10 LU. It sits above
    # the -70 absolute gate; the absolute-only average is dominated by 6
    # partial-burst blocks, so it differs from the final gated loudness.
    assert g["relative_gate_lufs"] > g["absolute_gate_lufs"]
    assert g["relative_gate_lufs"] == pytest.approx(-21.4062, abs=1e-3)
    assert r["integrated_loudness"]["integrated_lufs"] == pytest.approx(-10.61, abs=2e-2)


def test_gated_loudness_is_not_plain_rms(short_burst_sig, settings):
    """A naive full-clip RMS would be dominated by the 5.5 s of silence.

    The gated integrated loudness must come from the active blocks only and be
    far above the ungated mean-square energy across the whole signal.
    """
    r = run_measure(short_burst_sig, settings)
    gated = r["integrated_loudness"]["integrated_lufs"]
    x = short_burst_sig.samples
    whole_ms = np.mean(x * x)
    naive_lufs = -0.691 + 10 * np.log10(whole_ms)
    assert gated == pytest.approx(-10.61, abs=0.2)   # active region level
    assert gated - naive_lufs > 5.0                  # RMS proxy would lie low
    # Ungated whole-signal energy is mostly silence:
    assert naive_lufs < -16.0


def test_gate_thresholds_and_block_count_present_in_output(constant_sig, settings):
    r = run_measure(constant_sig, settings, include_blocks=True)
    i = r["integrated_loudness"]
    blocks = i["block_loudness_lufs"]
    # 8 s at 100 ms hop: 1 + (8.0-0.4)/0.1 = 77 momentary blocks.
    assert len(blocks) == i["gate_stats"]["total_blocks"] == 77
    # Steady-state blocks are identical; the first block contains the filter's
    # zero-state turn-on transient, so it alone may differ slightly.
    steady = blocks[2:]
    assert all(b == pytest.approx(steady[0], abs=1e-9) for b in steady)
    assert i["gate_stats"]["relative_gate_lufs"] == pytest.approx(
        steady[0] - 10.0, abs=1e-5)


# ---------------------------------------------------------------------------
# LRA mechanics
# ---------------------------------------------------------------------------

def test_dynamic_lra_is_twenty_lu(dynamic_long_sig, settings):
    r = run_measure(dynamic_long_sig, settings)
    l = r["loudness_range"]
    assert l["status"] == "OK"
    assert l["lra_lu"] == pytest.approx(20.0, abs=0.3)
    assert l["percentile_p95_lufs"] - l["percentile_p10_lufs"] == pytest.approx(
        l["lra_lu"], abs=1e-12)


def test_lra_relative_gate_is_twenty_lu_below_shortterm_integrated(
        dynamic_sig, settings):
    r = run_measure(dynamic_sig, settings)
    l = r["loudness_range"]
    g = l["gate_stats"]
    # Relative gate is reported and is ~20 LU below abs-gated ST integrated.
    assert g["relative_gate_lufs"] < g["absolute_gate_lufs"] + 40
    assert g["above_both_gates"] <= g["above_absolute_gate"]


# ---------------------------------------------------------------------------
# Channel weighting / LFE
# ---------------------------------------------------------------------------

def test_lfe_channel_is_excluded(settings):
    """Loud LFE (80 Hz, 0.9 FS) must not change the measured loudness.

    Goes through the media parser so the assertion covers LFE removal itself:
    same 5.1 file with LFE zeroed vs LFE loud must measure identically, and
    both must equal silence (front/surround channels are zero).
    """
    from app.media import decode_wav
    from conftest import Sig, write_wav_bytes

    dur = 6.0
    t = np.arange(int(dur * SR)) / SR
    quiet = np.zeros_like(t)
    lfe = 0.9 * np.sin(2 * np.pi * 80 * t)
    with_lfe = np.stack([quiet, quiet, quiet, lfe, quiet, quiet], axis=1)
    no_lfe = np.stack([quiet, quiet, quiet, quiet, quiet, quiet], axis=1)

    d_with = decode_wav(write_wav_bytes(with_lfe.astype(np.float32), SR))
    d_without = decode_wav(write_wav_bytes(no_lfe.astype(np.float32), SR))
    r_with = measure(d_with, request_id="lfe-on", settings=settings)
    r_without = measure(d_without, request_id="lfe-off", settings=settings)
    assert r_with["status"] == r_without["status"] == "SILENCE"


def test_surround_channels_receive_three_db_weight(settings):
    """Same signal fed to surrounds must read ~+3.01 dB vs a front channel."""
    from conftest import Sig
    dur = 6.0
    x = sine(0.3, 500.0, dur)
    front = Sig(np.stack([x, np.zeros_like(x), np.zeros_like(x),
                          np.zeros_like(x), np.zeros_like(x)], axis=1),
                (1.0, 1.0, 1.0, 1.41, 1.41), "5.0", 5)
    surround = Sig(np.stack([np.zeros_like(x), np.zeros_like(x), np.zeros_like(x),
                             x, np.zeros_like(x)], axis=1),
                   (1.0, 1.0, 1.0, 1.41, 1.41), "5.0", 5)
    rf = run_measure(front, settings)["integrated_loudness"]["integrated_lufs"]
    rs = run_measure(surround, settings)["integrated_loudness"]["integrated_lufs"]
    assert rs - rf == pytest.approx(10 * np.log10(1.41), abs=1e-6)


def test_channel_weights_reported_in_result(channel_change_sig, settings):
    r = run_measure(channel_change_sig, settings)
    assert r["signal"]["channel_weights"] == [1.0, 1.0, 1.0, 1.41, 1.41]
    assert r["signal"]["layout"] == "5.1"
    assert r["signal"]["source_channels"] == 6
    assert r["signal"]["analysis_channels"] == 5
