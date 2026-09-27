"""Report assembly: residuals, usable interval, and explicit caveats."""

from __future__ import annotations

from typing import Any

from .. import PIPELINE_VERSION
from .drift import DriftEstimate


def estimate_to_dict(est: DriftEstimate) -> dict[str, Any]:
    return {
        "offset_s": est.offset_s,
        "offset_std_s": est.offset_std_s,
        "drift_ppm": est.drift_ppm,
        "drift_std_ppm": est.drift_std_ppm,
        "fit_method": est.fit_method,
        "n_inliers": est.n_inliers,
        "n_outliers": est.n_outliers,
        "residual_rms_s": est.residual_rms_s,
        "residual_max_s": est.residual_max_s,
        "usable_interval_s": {
            "start": est.usable_interval_s[0],
            "end": est.usable_interval_s[1],
            "note": (
                "the drift model is validated only between the first and "
                "last inlier sync point; outside this span it is extrapolation"
            ),
        },
        "sync_points": [
            {
                "ref_time_s": p.ref_time_s,
                "measured_time_s": p.measured_time_s,
                "offset_s": p.offset_s,
                "residual_s": p.residual_s,
                "peak_score": p.peak_score,
                "inlier": p.inlier,
            }
            for p in est.points
        ],
    }


def base_report(job_id: str, request_id: str) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "request_id": request_id,
        "pipeline_version": PIPELINE_VERSION,
        # Correlation peaks establish *relative* alignment between the two
        # recordings. Nothing here verifies either clock against an absolute
        # time source, and the report must say so explicitly.
        "absolute_time_verified": False,
        "absolute_time_note": (
            "sync points come from waveform cross-correlation; they prove "
            "relative alignment of the two recordings, not absolute time"
        ),
    }
