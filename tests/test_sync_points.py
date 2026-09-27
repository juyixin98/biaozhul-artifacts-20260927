"""Sync-point detection unit tests (fixture-generated signals, known times)."""

import numpy as np
import pytest

from driftcorr.core.sync_points import (
    NoSyncPointsError, detect_sync_points, normalized_xcorr,
)
from driftcorr.fixtures.synth import chirp

FS = 8000
DETECT_KW = dict(threshold=0.55, max_pairing_offset_s=0.4,
                 min_peak_separation_s=0.05)


def _signal_with_pulses(pulse_times, n=FS * 4, f0=1200.0, f1=3200.0, dur=0.08):
    t = np.arange(n) / FS
    sig = np.zeros(n)
    for p in pulse_times:
        sig += chirp(t - p, f0, f1, dur)
    return sig


def test_detects_pulses_at_known_times():
    times = [0.5, 1.7, 3.1]
    sig = _signal_with_pulses(times)
    template = chirp(np.arange(0, 0.08, 1 / FS), 1200.0, 3200.0, 0.08)
    pts = detect_sync_points(sig, FS, template, times, **DETECT_KW)
    assert len(pts) == 3
    for p, t in zip(pts, times):
        assert p.ref_time_s == t
        assert p.measured_time_s == pytest.approx(t, abs=2e-3)
        assert p.peak_score > 0.9


def test_offset_pulses_report_the_offset():
    # Pulses actually sit 120 ms later than declared: every sync point must
    # report ~+0.120 s of offset (this is what feeds the drift fit).
    declared = [0.5, 1.5, 2.5]
    actual = [t + 0.120 for t in declared]
    sig = _signal_with_pulses(actual)
    template = chirp(np.arange(0, 0.08, 1 / FS), 1200.0, 3200.0, 0.08)
    pts = detect_sync_points(sig, FS, template, declared, **DETECT_KW)
    assert len(pts) == 3
    for p in pts:
        assert p.offset_s == pytest.approx(0.120, abs=2e-3)


def test_no_pulses_raises_no_sync_points():
    sig = np.zeros(FS * 2)
    template = chirp(np.arange(0, 0.08, 1 / FS), 1200.0, 3200.0, 0.08)
    with pytest.raises(NoSyncPointsError):
        detect_sync_points(sig, FS, template, [0.5, 1.0], **DETECT_KW)


def test_normalized_xcorr_peak_at_template_position():
    template = chirp(np.arange(0, 0.08, 1 / FS), 1200.0, 3200.0, 0.08)
    sig = np.zeros(FS)
    sig[1000:1000 + len(template)] = template
    corr = normalized_xcorr(sig, template)
    assert int(np.argmax(corr)) == 1000
    assert corr[1000] == pytest.approx(1.0, abs=1e-9)
