"""Drift estimation unit tests with literal, hand-computed sync points."""

import pytest

from driftcorr.core.drift import estimate_drift
from driftcorr.core.fit import InsufficientEvidenceError
from driftcorr.core.sync_points import SyncPoint

FIT_KW = dict(min_inliers=3, min_residual_threshold_s=0.004, mad_multiplier=6.0)


def _points(offsets, times=(1.0, 2.0, 3.0, 4.0, 5.0)):
    return [
        SyncPoint(ref_time_s=t, measured_time_s=t + o, offset_s=o, peak_score=0.9)
        for t, o in zip(times, offsets)
    ]


def test_known_offset_and_drift_recovered():
    # Ground truth: offset 120 ms, drift +80 ppm.
    # offsets computed by hand: 0.120 + 80e-6 * t
    offsets = [0.12008, 0.12016, 0.12024, 0.12032, 0.12040]
    est = estimate_drift(_points(offsets), **FIT_KW)
    assert est.offset_s == pytest.approx(0.120, abs=1e-9)
    assert est.drift_ppm == pytest.approx(80.0, abs=1e-6)
    assert est.n_inliers == 5
    assert est.n_outliers == 0
    assert est.residual_rms_s < 1e-9
    assert est.usable_interval_s == (1.0, 5.0)


def test_outlier_point_flagged_and_excluded():
    # Same truth as above; the point at t=3 is 40 ms late (a bad sync point).
    offsets = [0.12008, 0.12016, 0.16024, 0.12032, 0.12040]
    est = estimate_drift(_points(offsets), **FIT_KW)
    assert est.offset_s == pytest.approx(0.120, abs=1e-9)
    assert est.drift_ppm == pytest.approx(80.0, abs=1e-6)
    assert [p.inlier for p in est.points] == [True, True, False, True, True]
    bad = est.points[2]
    assert bad.residual_s == pytest.approx(0.040, abs=1e-6)
    # Usable interval still spans the inlier extremes.
    assert est.usable_interval_s == (1.0, 5.0)


def test_two_points_are_insufficient_evidence():
    offsets = [0.12008, 0.12040]
    with pytest.raises(InsufficientEvidenceError):
        estimate_drift(_points(offsets, times=(1.0, 5.0)), **FIT_KW)


def test_scattered_points_are_insufficient_evidence():
    # No consistent line through these; correction must be refused.
    offsets = [0.10, 0.30, 0.05, 0.45, 0.02]
    with pytest.raises(InsufficientEvidenceError):
        estimate_drift(_points(offsets), **FIT_KW)
