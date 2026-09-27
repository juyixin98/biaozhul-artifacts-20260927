"""Drift estimation: turn sync points into an offset + drift-ppm estimate."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .fit import FitResult, InsufficientEvidenceError, robust_line_fit
from .sync_points import SyncPoint


@dataclass(frozen=True)
class PointResidual:
    ref_time_s: float
    measured_time_s: float
    offset_s: float
    residual_s: float
    peak_score: float
    inlier: bool


@dataclass(frozen=True)
class DriftEstimate:
    offset_s: float                 # fixed offset at reference time 0
    drift_ppm: float                # relative clock rate error, parts per million
    offset_std_s: float             # rough 1-sigma uncertainty of the offset
    drift_std_ppm: float            # rough 1-sigma uncertainty of the drift
    points: list[PointResidual]
    residual_rms_s: float           # RMS over inliers only
    residual_max_s: float           # max |residual| over inliers only
    usable_interval_s: tuple[float, float]  # span covered by inlier sync points
    fit_method: str

    @property
    def n_inliers(self) -> int:
        return sum(1 for p in self.points if p.inlier)

    @property
    def n_outliers(self) -> int:
        return sum(1 for p in self.points if not p.inlier)


def estimate_drift(
    points: list[SyncPoint],
    *,
    min_inliers: int,
    min_residual_threshold_s: float,
    mad_multiplier: float,
    max_inlier_residual_s: float = 0.05,
) -> DriftEstimate:
    """Estimate fixed offset and drift from measured sync points.

    Raises InsufficientEvidenceError when the robust fit cannot find enough
    consistent points — in that case no correction may be applied.
    """
    x = np.array([p.ref_time_s for p in points], dtype=np.float64)
    y = np.array([p.offset_s for p in points], dtype=np.float64)

    fit: FitResult = robust_line_fit(
        x, y,
        min_inliers=min_inliers,
        min_threshold_s=min_residual_threshold_s,
        mad_multiplier=mad_multiplier,
        max_residual_s=max_inlier_residual_s,
    )

    inl = fit.inlier_mask
    inlier_residuals = fit.residuals[inl]
    rms = float(np.sqrt(np.mean(inlier_residuals**2)))
    rmax = float(np.max(np.abs(inlier_residuals)))

    # Rough uncertainty of slope/intercept from inlier scatter (OLS-style
    # standard errors around the robust fit — indicative, not a guarantee).
    xi = x[inl]
    n = len(xi)
    sxx = float(np.sum((xi - xi.mean()) ** 2))
    if n > 2 and sxx > 0:
        sigma2 = float(np.sum(inlier_residuals**2) / (n - 2))
        slope_std = float(np.sqrt(sigma2 / sxx))
        intercept_std = float(np.sqrt(sigma2 * (1.0 / n + xi.mean() ** 2 / sxx)))
    else:
        slope_std = float("nan")
        intercept_std = float("nan")

    point_rows = [
        PointResidual(
            ref_time_s=float(x[i]),
            measured_time_s=float(points[i].measured_time_s),
            offset_s=float(y[i]),
            residual_s=float(fit.residuals[i]),
            peak_score=float(points[i].peak_score),
            inlier=bool(fit.inlier_mask[i]),
        )
        for i in range(len(points))
    ]

    return DriftEstimate(
        offset_s=float(fit.intercept),
        drift_ppm=float(fit.slope * 1e6),
        offset_std_s=intercept_std,
        drift_std_ppm=float(slope_std * 1e6),
        points=point_rows,
        residual_rms_s=rms,
        residual_max_s=rmax,
        usable_interval_s=(float(xi.min()), float(xi.max())),
        fit_method=fit.method,
    )
