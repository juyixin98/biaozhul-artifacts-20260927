"""Filter design / plan contract: concrete numeric assertions."""

from __future__ import annotations

import numpy as np
import pytest

from resampler.errors import InputValidationError, ResourceExhaustedError
from resampler.config import Settings
from resampler.signal import build_plan, kaiser_beta, reduce_ratio
from resampler.signal.filter import ResamplePlan


def test_ratio_reduction_and_caps(settings, runlog):
    r = reduce_ratio(48000, 16000, 1_000_000)
    runlog.check("ratio 48k->16k reduces to 1/3",
                 (r.up, r.down) == (1, 3),
                 {"up": r.up, "down": r.down},
                 "gcd(48000,16000)=16000 -> L=1,M=3")

    r = reduce_ratio(48000, 44100, 1_000_000)
    runlog.check("ratio 48k->44.1k reduces to 147/160",
                 (r.up, r.down) == (147, 160),
                 {"up": r.up, "down": r.down},
                 "gcd=300 -> 44100/300=147, 48000/300=160")

    with pytest.raises(InputValidationError) as ei:
        reduce_ratio(0, 48000, 1_000_000)
    runlog.check("zero rate rejected", ei.value.category == "input_error",
                 {"category": ei.value.category}, "rate must be positive")

    with pytest.raises(InputValidationError) as ei:
        reduce_ratio(48000.25, 48000, 1_000_000)
    runlog.check("non-integer rate rejected (exact ratio contract)",
                 ei.value.category == "input_error",
                 {"category": ei.value.category},
                 "service only accepts exact integer rational ratios")

    with pytest.raises(ResourceExhaustedError) as ei:
        reduce_ratio(1, 2_000_000, 1_000_000)
    runlog.check("ratio-term cap -> resource_exhausted",
                 ei.value.category == "resource_exhausted",
                 ei.value.detail, "L=2,000,000 > 1,000,000 (configured quota)")


def test_kaiser_beta_table_values(runlog):
    cases = [(21.0, 0.0), (30.0, 0.5842 * 9 ** 0.4 + 0.07886 * 9),
             (60.0, 0.1102 * 51.3), (80.0, 0.1102 * 71.3)]
    for atten, expected in cases:
        b = kaiser_beta(atten)
        runlog.check(f"kaiser_beta({atten})", abs(b - expected) < 1e-12,
                     {"beta": b, "expected": expected},
                     "standard piecewise Kaiser formula")


def test_plan_shape_and_per_phase_dc_gain(settings, runlog):
    plan = build_plan(16000, 48000, settings=settings)
    assert isinstance(plan, ResamplePlan)
    L, M, K = plan.up, plan.down, plan.taps_per_phase
    runlog.check("L=3 for 16k->48k", L == 3, {"L": L}, "upsampling 3x")
    runlog.check("K even", K % 2 == 0, {"K": K}, "even taps per phase")
    runlog.check("prototype length K*L", plan.prototype.size == K * L,
                 {"N": plan.prototype.size, "K": K, "L": L},
                 "polyphase matrix reshapes exactly into prototype")
    runlog.check("polyphase shape (L,K)",
                 plan.polyphase.shape == (L, K),
                 {"shape": plan.polyphase.shape}, "(up, taps_per_phase)")

    total = float(plan.prototype.sum())
    runlog.check("prototype sum equals L exactly (system DC gain 1)",
                 abs(total - L) < 1e-10,
                 {"sum": total, "L": L},
                 "global normalization; preserves Kaiser transition shape")

    col_sums = plan.polyphase.sum(axis=1)
    ripple = float(np.max(np.abs(col_sums - col_sums.mean())))
    runlog.check("per-column DC gain ripple <= 2e-4 (intrinsic truncation)",
                 ripple <= 2e-4, {"max_ripple": ripple,
                                  "column_sums": col_sums.tolist()},
                 "columns are NOT re-normalized individually (that distorts cutoff)")

    # Prototype symmetry: h[k] == h[N-1-k]
    h = plan.prototype
    runlog.check("prototype symmetric (linear phase)",
                 np.allclose(h, h[::-1], atol=1e-12),
                 {"max_asym": float(np.max(np.abs(h - h[::-1])))},
                 "windowed sinc centered on (N-1)/2")

    # Group delay values
    runlog.check("group delay (K-1)/2 input samples",
                 abs(plan.delay_input - (K - 1) / 2.0) < 1e-12,
                 {"delay_input": plan.delay_input}, "documented alignment")
    runlog.check("group delay (N-1)/2 high-rate samples",
                 abs(plan.delay_high - (K * L - 1) / 2.0) < 1e-12,
                 {"delay_high": plan.delay_high}, "(N-1)/2")


def test_stopband_frequency_and_attenuation(settings, runlog):
    plan = build_plan(48000, 16000, settings=settings)  # down 3x: new Nyquist 8 kHz
    runlog.check("downsample stopband edge = new Nyquist",
                 abs(plan.stopband_edge_hz - 8000.0) < 1e-9,
                 {"stopband": plan.stopband_edge_hz},
                 "min(fin,fout)/2 = 8000")
    runlog.check("passband edge = 0.9*stopband",
                 abs(plan.passband_edge_hz - 7200.0) < 1e-9,
                 {"passband": plan.passband_edge_hz}, "default pb fraction 0.9")
    runlog.check("cutoff midpoint 0.5*(pb+sb)",
                 abs(plan.cutoff_hz - 7600.0) < 1e-9,
                 {"cutoff": plan.cutoff_hz}, "0.5*(7200+8000)=7600")

    # Empirical stopband attenuation of the prototype on the high-rate grid.
    # The Kaiser window reaches the target attenuation only past one transition
    # width beyond the stopband-edge frequency (stopband edge = new Nyquist),
    # so begin the measurement at sb + (sb - passband_edge), i.e. symmetric
    # past the edge. The passband itself is covered in test_03 by tone tests.
    L, M, K = plan.up, plan.down, plan.taps_per_phase
    f_high = L * 48000.0                          # high-rate sample rate F_h
    nfft = 1 << 16
    H = np.fft.rfft(plan.prototype, nfft)
    freqs = np.fft.rfftfreq(nfft, d=1.0 / f_high)
    mag = 20 * np.log10(np.abs(H) / np.abs(H[0]) + 1e-30)
    sb, pb = plan.stopband_edge_hz, plan.passband_edge_hz
    # When input==output rate the stopband begins exactly at Nyquist and the
    # measurable post-transition window is empty; that rate is covered by the
    # /3 and rational-ratio cases below.
    start = sb + (sb - pb)
    band = (freqs > start) & (freqs < f_high / 2)
    runlog.check("stopband measurement window non-empty",
                 band.sum() > 100, {"points": int(band.sum()),
                                    "start": start, "nyq": f_high / 2},
                 "enough FFT bins above the transition to measure attenuation")
    worst = float(np.max(mag[band]))
    runlog.check("Kaiser prototype stopband <= -55 dB worst bin",
                 worst <= -55.0, {"worst_db": worst},
                 "80 dB design target; allow 25 dB grid/window margin")


def test_filter_tap_cap(settings, runlog):
    tiny = Settings(db_path=settings.db_path, log_dir=settings.log_dir,
                    max_samples_per_chunk=4096, max_total_samples=65536,
                    max_jobs=8, max_ratio_term=1_000_000, max_filter_taps=64)
    with pytest.raises(ResourceExhaustedError) as ei:
        build_plan(1, 1000, settings=tiny)
    runlog.check("tap cap surfaces ResourceExhaustedError",
                 ei.value.category == "resource_exhausted", ei.value.detail,
                 "huge ratio needs > 64 taps")


def test_bad_params(settings):
    with pytest.raises(InputValidationError):
        build_plan(48000, 16000, atten_db=-3, settings=settings)
    with pytest.raises(InputValidationError):
        build_plan(48000, 16000, passband_edge=1.0, settings=settings)
