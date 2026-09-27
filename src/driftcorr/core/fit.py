"""Robust linear fit of clock offset vs. reference time.

Model: offset(t) = intercept + slope * t, where slope is the relative clock
drift (dimensionless; ppm = slope * 1e6) and intercept is the fixed offset.

Method: Theil–Sen median-slope estimator, followed by MAD-based inlier
selection and one refit on the inliers. This gives a deterministic, fully
explicit robust fit — outliers are named, never silently averaged in.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


class InsufficientEvidenceError(Exception):
    """Raised when too few inliers remain to support a correction."""


@dataclass(frozen=True)
class FitResult:
    slope: float
    intercept: float
    inlier_mask: np.ndarray          # bool array over input points
    residuals: np.ndarray            # residual of every input point
    mad_s: float                     # scaled MAD of inlier residuals
    threshold_s: float               # inlier cutoff actually used
    method: str = "theil_sen+mad"

    @property
    def n_inliers(self) -> int:
        return int(self.inlier_mask.sum())


def _theil_sen(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    slopes = []
    for i in range(len(x)):
        dx = x[i + 1:] - x[i]
        valid = np.abs(dx) > 1e-12
        slopes.extend(((y[i + 1:] - y[i])[valid] / dx[valid]).tolist())
    if not slopes:
        raise InsufficientEvidenceError("degenerate abscissae: cannot fit a slope")
    slope = float(np.median(slopes))
    intercept = float(np.median(y - slope * x))
    return slope, intercept


def robust_line_fit(
    x: np.ndarray,
    y: np.ndarray,
    *,
    min_inliers: int,
    min_threshold_s: float,
    mad_multiplier: float,
    max_residual_s: float | None = None,
) -> FitResult:
    """Fit y = slope*x + intercept robustly.

    Raises InsufficientEvidenceError when fewer than `min_inliers` points
    survive outlier rejection, or when `max_residual_s` is given and the
    inlier residual RMS exceeds it (the points are then inconsistent with
    the detection precision, so no correction may be applied).
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if len(x) != len(y):
        raise ValueError("x and y must have equal length")
    if len(x) < 2:
        raise InsufficientEvidenceError(
            f"need at least 2 points to fit, got {len(x)}"
        )

    slope, intercept = _theil_sen(x, y)
    residuals = y - (intercept + slope * x)

    mad = float(np.median(np.abs(residuals - np.median(residuals))))
    scaled_mad = 1.4826 * mad
    threshold = max(min_threshold_s, mad_multiplier * scaled_mad)
    inlier_mask = np.abs(residuals) <= threshold

    if inlier_mask.sum() >= 2 and not inlier_mask.all():
        # One refit on inliers only; recompute mask against the refit line.
        slope, intercept = _theil_sen(x[inlier_mask], y[inlier_mask])
        residuals = y - (intercept + slope * x)
        inlier_resid = residuals[inlier_mask]
        mad = float(np.median(np.abs(inlier_resid - np.median(inlier_resid))))
        scaled_mad = 1.4826 * mad
        threshold = max(min_threshold_s, mad_multiplier * scaled_mad)
        inlier_mask = np.abs(residuals) <= threshold

    if int(inlier_mask.sum()) < min_inliers:
        raise InsufficientEvidenceError(
            f"only {int(inlier_mask.sum())} inlier(s) survive robust fitting "
            f"(min_inliers={min_inliers}); evidence is insufficient to correct"
        )

    if max_residual_s is not None:
        inlier_rms = float(np.sqrt(np.mean(residuals[inlier_mask] ** 2)))
        if inlier_rms > max_residual_s:
            raise InsufficientEvidenceError(
                f"inlier residual RMS {inlier_rms:.4f}s exceeds the plausible "
                f"detection error bound {max_residual_s:.4f}s; the sync points "
                "are mutually inconsistent and no correction is applied"
            )

    return FitResult(
        slope=slope,
        intercept=intercept,
        inlier_mask=inlier_mask,
        residuals=residuals,
        mad_s=scaled_mad,
        threshold_s=threshold,
    )
