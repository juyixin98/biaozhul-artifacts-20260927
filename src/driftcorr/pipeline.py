"""Pipeline orchestration: media -> sync points -> robust fit -> outputs.

Outputs are deliberately split in two:
  1. a resampled ("corrected") WAV rendered onto the reference sample grid;
  2. a metadata time mapping (offset + drift applied to event timestamps),
     reported as numbers, independent of any audio rendering.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np

from .config import AppConfig
from .logging_utils import get_logger, log_step
from .media.metadata import MediaMetadata
from .media.wav_io import AudioData, write_wav
from .core.drift import estimate_drift
from .core.fit import InsufficientEvidenceError
from .core.report import base_report, estimate_to_dict
from .core.resample import correct_clock
from .core.sync_points import NoSyncPointsError, detect_sync_points
from .core.timeline import TimeMapping, map_events

logger = get_logger("pipeline")


class PipelineError(Exception):
    """Expected, classifiable pipeline failure (reported on the job)."""

    error_class = "pipeline_error"


class NoSyncPoints(PipelineError):
    error_class = "no_sync_points"


class InsufficientEvidence(PipelineError):
    error_class = "insufficient_evidence"


def _sha256_short(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _extract_template(reference: AudioData, pulse_time_s: float,
                      pulse_duration_s: float) -> np.ndarray:
    i0 = int(pulse_time_s * reference.sample_rate)
    i1 = i0 + int(pulse_duration_s * reference.sample_rate)
    if i0 < 0 or i1 > len(reference.samples):
        raise PipelineError(f"template window [{i0}, {i1}) outside reference audio")
    return reference.samples[i0:i1].copy()


def _alignment_check(corrected: np.ndarray, fs: int, template: np.ndarray,
                     expected_times_s: list[float], cfg: AppConfig,
                     max_time_s: float) -> dict[str, Any]:
    """Re-detect pulses in the corrected audio and measure residual lag."""
    times = [t for t in expected_times_s if t < max_time_s - 0.05]
    if not times:
        return {"enabled": True, "skipped": "no expected pulses inside corrected audio"}
    try:
        points = detect_sync_points(
            corrected, fs, template, times,
            threshold=cfg.detection.correlation_threshold,
            max_pairing_offset_s=0.1,  # corrected audio should be nearly aligned
            min_peak_separation_s=cfg.detection.min_peak_separation_s,
        )
    except NoSyncPointsError:
        return {"enabled": True, "skipped": "no pulses re-detected in corrected audio"}
    residuals_ms = [abs(p.measured_time_s - p.ref_time_s) * 1e3 for p in points]
    return {
        "enabled": True,
        "n_checked": len(points),
        "residual_rms_ms": float(np.sqrt(np.mean(np.square(residuals_ms)))),
        "residual_max_ms": float(np.max(residuals_ms)),
        "note": "residual lag of re-detected sync pulses after correction",
    }


def run_pipeline(
    *,
    job_id: str,
    request_id: str,
    reference: AudioData,
    target: AudioData,
    reference_meta: MediaMetadata,
    target_meta: MediaMetadata | None,
    cfg: AppConfig,
) -> dict[str, Any]:
    """Run the full estimation/correction pipeline. Raises PipelineError."""
    if reference_meta.pulses is None:
        raise PipelineError("reference metadata declares no sync pulses")
    if reference.sample_rate != target.sample_rate:
        raise PipelineError(
            f"sample-rate mismatch: reference {reference.sample_rate} Hz vs "
            f"target {target.sample_rate} Hz"
        )
    fs = reference.sample_rate
    pulses = reference_meta.pulses

    report = base_report(job_id, request_id)
    report["inputs"] = {
        "reference": {"path": reference.source_path,
                      "sha256_16": _sha256_short(reference.source_path),
                      "duration_s": reference.duration_s, "sample_rate": fs},
        "target": {"path": target.source_path,
                   "sha256_16": _sha256_short(target.source_path),
                   "duration_s": target.duration_s, "sample_rate": fs},
    }

    template = _extract_template(reference, pulses.times_s[0], pulses.duration_s)
    log_step(logger, request_id=request_id, job_id=job_id,
             step="template", message="template extracted from reference",
             pulse_time_s=pulses.times_s[0], duration_s=pulses.duration_s)

    try:
        points = detect_sync_points(
            target.samples, fs, template, pulses.times_s,
            threshold=cfg.detection.correlation_threshold,
            max_pairing_offset_s=cfg.detection.max_pairing_offset_s,
            min_peak_separation_s=cfg.detection.min_peak_separation_s,
        )
    except NoSyncPointsError as exc:
        raise NoSyncPoints(str(exc)) from exc
    log_step(logger, request_id=request_id, job_id=job_id,
             step="detect", message="sync points detected",
             n_points=len(points), n_expected=len(pulses.times_s))

    try:
        est = estimate_drift(
            points,
            min_inliers=cfg.fit.min_inliers,
            min_residual_threshold_s=cfg.fit.min_residual_threshold_s,
            mad_multiplier=cfg.fit.mad_multiplier,
            max_inlier_residual_s=cfg.fit.max_inlier_residual_s,
        )
    except InsufficientEvidenceError as exc:
        raise InsufficientEvidence(str(exc)) from exc
    log_step(logger, request_id=request_id, job_id=job_id,
             step="fit", message="robust drift fit complete",
             offset_s=f"{est.offset_s:.6f}", drift_ppm=f"{est.drift_ppm:.3f}",
             inliers=est.n_inliers, outliers=est.n_outliers,
             residual_rms_ms=f"{est.residual_rms_s * 1e3:.3f}")

    report["estimate"] = estimate_to_dict(est)

    # --- Output 1: resampled audio on the reference grid ---
    corrected = correct_clock(
        target.samples, fs,
        offset_s=est.offset_s, drift_ppm=est.drift_ppm,
        half_width=cfg.resample.half_width_taps,
    )
    out_dir = Path(cfg.output_dir) / job_id
    out_path = out_dir / "corrected.wav"
    write_wav(out_path, corrected.samples, fs)
    report["corrected_audio"] = {
        "path": str(out_path),
        "duration_s": len(corrected.samples) / fs,
        "first_target_position_samples": corrected.first_target_position,
        "drift_ratio": corrected.drift_ratio,
    }
    log_step(logger, request_id=request_id, job_id=job_id,
             step="resample", message="corrected audio written",
             path=str(out_path), n_samples=len(corrected.samples))

    # --- Output 2: metadata time mapping (separate from audio) ---
    mapping = TimeMapping(offset_s=est.offset_s, drift_ppm=est.drift_ppm)
    mapping_dict: dict[str, Any] = {
        "offset_s": mapping.offset_s,
        "drift_ppm": mapping.drift_ppm,
        "formula": "t_ref = (t_target - offset_s) / (1 + drift_ppm * 1e-6)",
    }
    if target_meta and target_meta.events:
        mapped = map_events(target_meta.events, mapping, est.usable_interval_s)
        mapping_dict["events"] = [
            {
                "name": m.name,
                "target_time_s": m.target_time_s,
                "ref_time_s": m.ref_time_s,
                "within_usable_interval": m.within_usable_interval,
            }
            for m in mapped
        ]
    report["time_mapping"] = mapping_dict

    # --- Alignment residual on the corrected audio ---
    if cfg.report.alignment_check_enabled:
        report["alignment_check"] = _alignment_check(
            corrected.samples, fs, template, pulses.times_s, cfg,
            max_time_s=len(corrected.samples) / fs,
        )
        ac = report["alignment_check"]
        log_step(logger, request_id=request_id, job_id=job_id,
                 step="alignment_check", message="post-correction residual",
                 residual_rms_ms=ac.get("residual_rms_ms"),
                 residual_max_ms=ac.get("residual_max_ms"))

    report["status"] = "ok"
    return report


def failure_report(job_id: str, request_id: str, exc: Exception) -> dict[str, Any]:
    report = base_report(job_id, request_id)
    error_class = getattr(exc, "error_class", type(exc).__name__)
    report["status"] = "failed"
    report["failure"] = {"error_class": error_class, "message": str(exc)}
    return report
