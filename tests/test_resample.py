"""Resampling unit tests. Reference answers come from the analytic form of
the test signal — never from the resampler itself."""

import numpy as np
import pytest

from driftcorr.core.resample import correct_clock, windowed_sinc_at

FS = 8000


def _sine(freq=440.0, n=4000, phase=0.3):
    t = np.arange(n) / FS
    return np.sin(2 * np.pi * freq * t + phase)


def test_fractional_shift_matches_analytic_signal():
    x = _sine()
    # Sample the sine 0.3 samples later: analytic answer is known exactly.
    positions = np.arange(64, 3936) + 0.3
    y = windowed_sinc_at(x, positions, half_width=16)
    t = positions / FS
    expected = np.sin(2 * np.pi * 440.0 * t + 0.3)
    err = y - expected
    assert np.sqrt(np.mean(err**2)) < 1e-3
    assert np.max(np.abs(err)) < 5e-3


def test_integer_positions_are_identity():
    x = _sine(freq=317.0)
    positions = np.arange(32, 3968, dtype=float)
    y = windowed_sinc_at(x, positions, half_width=16)
    np.testing.assert_allclose(y, x[32:3968], atol=1e-12)


def test_correct_clock_undoes_known_drift_and_offset():
    # Build a "target" by analytic evaluation on a drifted grid, then check
    # the corrector lands back on the reference grid.
    offset_s, drift_ppm = 0.050, 100.0
    drift = drift_ppm * 1e-6
    n = 8000
    t_tgt = np.arange(n) / FS
    freq, phase = 440.0, 0.3
    target = np.sin(2 * np.pi * freq * (t_tgt - offset_s) / (1 + drift) + phase)

    corrected = correct_clock(target, FS, offset_s=offset_s,
                              drift_ppm=drift_ppm, half_width=16)
    # Output sample n must equal the analytic reference at time
    # (first_target_position/fs ... ) mapped back to the reference grid.
    t_out = (np.arange(len(corrected.samples))
             + (corrected.first_target_position - offset_s * FS)
             / (1 + drift)) / FS
    expected = np.sin(2 * np.pi * freq * t_out + phase)
    err = corrected.samples - expected
    assert np.sqrt(np.mean(err**2)) < 1e-3


def test_correct_clock_empty_when_window_too_small():
    x = np.zeros(10)
    out = correct_clock(x, FS, offset_s=0.0, drift_ppm=0.0, half_width=16)
    assert len(out.samples) == 0
