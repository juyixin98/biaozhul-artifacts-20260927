"""Unit tests for robust clock fitting.

These build sync points by formula (not by the package's own generator) so the
fit is judged against independent truth and must both reject outliers and
split frame-drop discontinuities.
"""
from __future__ import annotations

import pytest

from clockalign.timefit import (SyncPoint, robust_fit)

KW = dict(min_points=4, min_span_s=3.0, ransac_iterations=2000,
          ransac_seed=1, inlier_threshold_s=0.002, min_inlier_fraction=0.6,
          min_inliers=4, max_drift_ppm=1000.0, segment_min_points=3,
          segment_min_span_s=1.0, discontinuity_jump_s=0.003)


def _line(points, ppm, offset):
    return [SyncPoint(t_a=t, t_b=(1 + ppm * 1e-6) * t + offset)
            for t in points]


def test_recovers_offset_and_drift_on_clean_line():
    pts = _line([2, 3.5, 5, 6.5, 8], ppm=200.0, offset=0.3)
    res = robust_fit(pts, **KW)
    assert res.status == "ok"
    assert res.drift_ppm == pytest.approx(200.0, abs=0.5)
    assert res.offset_s == pytest.approx(0.3, abs=1e-4)
    assert res.rms_residual_s < 1e-6
    assert len(res.segments) == 1


def test_rejects_outlier_sync_points():
    pts = _line([2, 3.5, 5, 6.5, 8, 9.5], ppm=100.0, offset=0.1)
    pts.append(SyncPoint(t_a=5.75, t_b=5.9))  # arbitrary false point
    pts.append(SyncPoint(t_a=7.25, t_b=7.1))
    res = robust_fit(pts, **KW)
    assert res.status == "ok"
    assert len(res.outlier_points) == 2
    assert res.drift_ppm == pytest.approx(100.0, abs=1.0)
    assert all(p.t_a in (5.75, 7.25) for p in res.outlier_points)


def test_insufficient_when_too_few_points():
    pts = _line([2.0, 3.0], ppm=50.0, offset=0.0)
    res = robust_fit(pts, **KW)
    assert res.status == "insufficient_evidence"
    assert res.global_model is None
    assert "sync point" in res.reason


def test_insufficient_when_span_too_short():
    pts = _line([2.0, 2.1, 2.2, 2.3, 2.4], ppm=50.0, offset=0.0)
    res = robust_fit(pts, **KW)
    assert res.status == "insufficient_evidence"
    assert "span" in res.reason


def test_rejects_implausibly_large_drift():
    kw = dict(KW)
    kw["max_drift_ppm"] = 500.0
    pts = _line([2, 3.5, 5, 6.5, 8], ppm=5000.0, offset=0.0)
    res = robust_fit(pts, **kw)
    assert res.status == "insufficient_evidence"
    assert "ppm" in res.reason


def test_splits_two_segments_at_frame_drop():
    pre = _line([2, 3.5, 5, 6.5], ppm=80.0, offset=-0.12)
    post_t = [8.0, 9.5, 11.0]
    post = [SyncPoint(t_a=t,
                      t_b=1.00008 * t - 0.12 - 0.01) for t in post_t]
    res = robust_fit(pre + post, **KW)
    assert res.status == "ok"
    assert len(res.segments) == 2
    seg0, seg1 = res.segments
    assert seg0.t_a_end < 8.0 and seg1.t_a_start >= 8.0
    # Anchor segment recovers the true drift; both segment residuals tiny.
    assert seg0.model.drift_ppm == pytest.approx(80.0, abs=12.0)
    assert seg0.rms_residual_s < 1e-6
    assert seg1.rms_residual_s < 1e-6
    # The two segments differ in intercept by the 10 ms drop.
    assert abs((seg1.model.intercept - seg0.model.intercept) + 0.01) < 2e-3


def test_all_inconsistent_points_is_insufficient():
    pts = [SyncPoint(t_a=2 + i, t_b=2.0 + (i % 3) * 0.3) for i in range(6)]
    res = robust_fit(pts, **KW)
    assert res.status == "insufficient_evidence"
