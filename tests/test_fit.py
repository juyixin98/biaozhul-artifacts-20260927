"""Robust-fit unit tests with hand-computed expected values.

Note on sample sizes: the Theil–Sen estimator breaks down when more than
~29% of the *points* are outliers, so the outlier cases below use enough
points to stay inside the estimator's guaranteed regime — the same regime
the pipeline requires via min_inliers.
"""

import numpy as np
import pytest

from driftcorr.core.fit import InsufficientEvidenceError, robust_line_fit

FIT_KW = dict(min_inliers=3, min_threshold_s=0.004, mad_multiplier=6.0)


def test_exact_line_recovered_exactly():
    # y = 2x + 1 by construction; any sane fit must return exactly this.
    x = np.array([0.0, 1.0, 2.0, 3.0])
    y = np.array([1.0, 3.0, 5.0, 7.0])
    res = robust_line_fit(x, y, **FIT_KW)
    assert res.slope == pytest.approx(2.0, abs=1e-12)
    assert res.intercept == pytest.approx(1.0, abs=1e-12)
    assert res.n_inliers == 4
    assert np.all(res.inlier_mask)


def test_single_gross_outlier_is_named_not_averaged():
    # y = 2x + 1 on x = 0..5, with the point at x=3 corrupted to 70.
    # Hand-computed expectation: slope 2, intercept 1, index 3 flagged.
    x = np.arange(6.0)
    y = np.array([1.0, 3.0, 5.0, 70.0, 9.0, 11.0])
    res = robust_line_fit(x, y, **FIT_KW)
    assert res.slope == pytest.approx(2.0, abs=1e-9)
    assert res.intercept == pytest.approx(1.0, abs=1e-9)
    assert res.inlier_mask.tolist() == [True, True, True, False, True, True]
    assert abs(res.residuals[3]) > 0.1


def test_two_outliers_among_eight_still_recovered():
    # True model y = 0.5x - 1 on x = 0..7; points 2 and 5 corrupted.
    x = np.arange(8.0)
    y = np.array([-1.0, -0.5, 9.0, 0.5, 1.0, -8.0, 2.0, 2.5])
    res = robust_line_fit(x, y, **FIT_KW)
    assert res.slope == pytest.approx(0.5, abs=1e-9)
    assert res.intercept == pytest.approx(-1.0, abs=1e-9)
    assert res.inlier_mask.tolist() == [
        True, True, False, True, True, False, True, True]


def test_two_points_are_insufficient():
    with pytest.raises(InsufficientEvidenceError):
        robust_line_fit(np.array([0.0, 1.0]), np.array([1.0, 3.0]), **FIT_KW)


def test_mutually_inconsistent_points_fail_the_residual_bound():
    # No plausible line: even the best robust fit leaves residuals far above
    # any conceivable detection error. With max_residual_s set, the fitter
    # must refuse rather than return a meaningless line.
    x = np.arange(5.0)
    y = np.array([0.0, 0.5, 9.0, -7.0, 25.0])
    with pytest.raises(InsufficientEvidenceError, match="residual RMS"):
        robust_line_fit(x, y, max_residual_s=0.05, **FIT_KW)
