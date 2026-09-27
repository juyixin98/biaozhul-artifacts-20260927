"""End-to-end alignment pipeline.

Orchestrates, with structured logging at every step:

1. media inputs (resolved by the caller via :mod:`clockalign.media`);
2. sync evidence -- known pulses, content correlation, or auto fallback;
3. robust RANSAC clock fit with segment splitting;
4. discontinuity (frame drop/duplicate) localization;
5. resampled audio (artifact) and the separate timeline metadata;
6. residual / usable-interval reporting, failures and uncertainties listed
   separately.

The pipeline returns a plain :class:`AlignmentResult`; persistence and HTTP
concerns live in other modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field


from . import __version__
from .config import Config
from .correlation import find_correlation_points
from .discontinuity import Cut, locate_all_cuts
from .errors import InsufficientEvidenceError
from .logging_setup import get_logger
from .media import AudioTrack
from .pulses import detect_pulses, pair_pulses, pulse_template
from .resample import Correction, correct_track
from .timefit import FitResult, SyncPoint, robust_fit

log = get_logger("pipeline")


@dataclass
class StepRecord:
    name: str
    status: str
    detail: dict = field(default_factory=dict)


@dataclass
class AlignmentResult:
    status: str                      # "corrected" | "uncorrected"
    failure: dict | None
    warnings: list[dict]
    uncertainties: list[dict]
    steps: list[StepRecord]
    estimate: dict | None
    residual_report: dict | None
    timeline_map: dict | None
    correction: Correction | None
    sync_points: dict
    mode_used: str | None
    external_time_anchor: bool
    version: str = __version__

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "version": self.version,
            "failure": self.failure,
            "warnings": self.warnings,
            "uncertainties": self.uncertainties,
            "steps": [{"name": s.name, "status": s.status, "detail": s.detail}
                      for s in self.steps],
            "estimate": self.estimate,
            "residual_report": self.residual_report,
            "sync_points": self.sync_points,
            "mode_used": self.mode_used,
            "external_time_anchor": self.external_time_anchor,
            "timeline_map": self.timeline_map,
        }


def _pulse_sync(reference: AudioTrack, slave: AudioTrack, cfg: Config,
                steps: list[StepRecord]):
    template = pulse_template(cfg.sync.pulse.frequency_hz,
                              cfg.sync.pulse.duration_s,
                              reference.sample_rate, cfg.sync.pulse.window)
    pa = detect_pulses(reference.samples, reference.sample_rate, template,
                       score_threshold=cfg.sync.pulse.score_threshold,
                       min_spacing_s=cfg.sync.pulse.min_spacing_s)
    pb = detect_pulses(slave.samples, slave.sample_rate, template,
                       score_threshold=cfg.sync.pulse.score_threshold,
                       min_spacing_s=cfg.sync.pulse.min_spacing_s)
    pairs, un_a, un_b = pair_pulses(
        pa, pb, max_offset_s=cfg.sync.pair_max_offset_s,
        inlier_threshold_s=cfg.fit.ransac.inlier_threshold_s,
        iterations=cfg.fit.ransac.iterations, seed=cfg.fit.ransac.seed)
    points = [SyncPoint(t_a=p.t_a, t_b=p.t_b,
                        score=float(min(p.score_a, p.score_b)),
                        source="pulse") for p in pairs]
    steps.append(StepRecord("sync.pulses", "ok", {
        "detected_reference": len(pa), "detected_slave": len(pb),
        "paired": len(pairs), "unmatched_reference": len(un_a),
        "unmatched_slave": len(un_b)}))
    return points, un_a, un_b


def _correlation_sync(reference: AudioTrack, slave: AudioTrack, cfg: Config,
                      steps: list[StepRecord]):
    cps = find_correlation_points(
        reference.samples, slave.samples, reference.sample_rate,
        window_s=cfg.sync.correlation.window_s,
        hop_s=cfg.sync.correlation.hop_s,
        search_half_window_s=cfg.sync.correlation.search_half_window_s,
        min_score=cfg.sync.correlation.min_score)
    points = [SyncPoint(t_a=p.t_a, t_b=p.t_b, score=p.score,
                        source="correlation") for p in cps]
    steps.append(StepRecord("sync.correlation", "ok", {
        "accepted_points": len(cps),
        "min_score": cfg.sync.correlation.min_score}))
    return points


def run_alignment(reference: AudioTrack, slave: AudioTrack, cfg: Config,
                  *, mode: str | None = None,
                  external_time_anchor: bool = False) -> AlignmentResult:
    """Run the full correction. Never raises for *evidence* failures:
    they are reported with status ``uncorrected`` and a failure category."""
    steps: list[StepRecord] = []
    warnings: list[dict] = []
    uncertainties: list[dict] = []
    steps.append(StepRecord("media.loaded", "ok", {
        "reference": {"source": reference.source,
                      "channel": reference.channel,
                      "sample_rate_hz": reference.sample_rate,
                      "samples": int(reference.samples.size),
                      "duration_s": reference.duration_s},
        "slave": {"source": slave.source, "channel": slave.channel,
                  "sample_rate_hz": slave.sample_rate,
                  "samples": int(slave.samples.size),
                  "duration_s": slave.duration_s}}))

    selected_mode = mode or cfg.sync.mode
    unmatched_a: list[float] = []
    unmatched_b: list[float] = []
    try:
        if selected_mode == "pulses":
            points, unmatched_a, unmatched_b = _pulse_sync(
                reference, slave, cfg, steps)
        elif selected_mode == "correlate":
            points = _correlation_sync(reference, slave, cfg, steps)
        elif selected_mode == "auto":
            points, unmatched_a, unmatched_b = _pulse_sync(
                reference, slave, cfg, steps)
            if len(points) < cfg.fit.min_points:
                warnings.append({
                    "code": "sync_fallback_to_correlation",
                    "message": f"only {len(points)} paired pulse(s); falling "
                               "back to content correlation"})
                points = _correlation_sync(reference, slave, cfg, steps)
        else:
            raise ValueError(f"unknown sync mode: {selected_mode}")
    except InsufficientEvidenceError:
        raise
    except Exception as exc:  # pragma: no cover - defensive boundary
        log.exception("sync extraction failed")
        return _uncorrected("sync_error", str(exc), steps, warnings,
                            uncertainties, external_time_anchor)

    fit: FitResult = robust_fit(
        points,
        min_points=cfg.fit.min_points, min_span_s=cfg.fit.min_span_s,
        ransac_iterations=cfg.fit.ransac.iterations,
        ransac_seed=cfg.fit.ransac.seed,
        inlier_threshold_s=cfg.fit.ransac.inlier_threshold_s,
        min_inlier_fraction=cfg.fit.ransac.min_inlier_fraction,
        min_inliers=cfg.fit.ransac.min_inliers,
        max_drift_ppm=cfg.fit.max_drift_ppm,
        segment_min_points=cfg.fit.segments.min_points,
        segment_min_span_s=cfg.fit.segments.min_span_s,
        discontinuity_jump_s=cfg.fit.segments.discontinuity_jump_s)
    steps.append(StepRecord("fit.robust", fit.status, {
        "sync_points_in": len(points),
        "inliers": len(fit.inlier_points),
        "outliers": len(fit.outlier_points),
        "segments": len(fit.segments),
        "reason": fit.reason}))

    if fit.status != "ok":
        return _uncorrected(fit.reason or "insufficient_evidence", fit.reason
                            or "insufficient evidence", steps, warnings,
                            uncertainties, external_time_anchor,
                            failure_code="insufficient_evidence",
                            points=points, unmatched_a=unmatched_a,
                            unmatched_b=unmatched_b)

    if fit.outlier_points:
        warnings.append({
            "code": "rejected_sync_outliers",
            "message": f"{len(fit.outlier_points)} sync point(s) rejected by "
                       "RANSAC as inconsistent with the clock model",
            "points": [{"t_a_s": p.t_a, "t_b_s": p.t_b, "source": p.source}
                       for p in fit.outlier_points]})
    if unmatched_b:
        warnings.append({
            "code": "unmatched_slave_pulses",
            "message": f"{len(unmatched_b)} pulse(s) seen only on the slave "
                       "device were excluded as false sync points",
            "times_s": sorted(float(t) for t in unmatched_b)})
    if unmatched_a:
        warnings.append({
            "code": "unmatched_reference_pulses",
            "message": f"{len(unmatched_a)} reference pulse(s) had no slave "
                       "match",
            "times_s": sorted(float(t) for t in unmatched_a)})

    cuts: list[Cut] = locate_all_cuts(
        reference.samples, slave.samples, reference.sample_rate,
        fit.segments)
    for cut in cuts:
        if cut.localization != "content_scan":
            uncertainties.append({
                "code": "cut_position_unlocalized",
                "message": "frame discontinuity position could not be pinned "
                           "by content scan; using the midpoint between "
                           "flanking sync points",
                "approx_reference_time_s": cut.t_a})
    steps.append(StepRecord("discontinuities.locate", "ok", {
        "cuts": [{"t_a_s": c.t_a, "kind": c.kind,
                  "localization": c.localization,
                  "margin": c.scan_score_margin} for c in cuts]}))

    correction = correct_track(
        slave.samples, reference.sample_rate, fit,
        reference_length=reference.samples.size,
        edge_guard_s=cfg.resample.edge_guard_s, cuts=cuts)
    steps.append(StepRecord("resample.correct", "ok", {
        "method": cfg.resample.method,
        "output_samples": int(correction.corrected_audio.size),
        "gaps": len(correction.gaps)}))

    if not external_time_anchor:
        uncertainties.append({
            "code": "relative_time_only",
            "message": "offset and drift are estimated from pulse/correlation "
                       "evidence on the two tracks' own timelines; a "
                       "correlation peak is not proof of absolute wall-clock "
                       "time. Attach an external time anchor to interpret the "
                       "offset as absolute."})
    per_seg = [{
        "segment_index": s.index,
        "usable_reference_range_s": [s.t_a_start, s.t_a_end],
        "n_sync_points": s.n_points,
        "rms_residual_s": s.rms_residual_s,
        "max_abs_residual_s": s.max_abs_residual_s,
        "drift_ppm": s.model.drift_ppm,
        "offset_s": s.model.intercept,
    } for s in fit.segments]
    residual_report = {
        "global": {
            "rms_residual_s": fit.rms_residual_s,
            "max_abs_residual_s": fit.max_abs_residual_s,
        },
        "segments": per_seg,
        "usable_reference_range_s": list(fit.usable_t_a_range),
        "output_reference_range_s": list(correction.output_t_a_range),
        "frame_gaps": [{
            "kind": g.kind,
            "reference_time_start_s": g.t_a_start,
            "reference_time_end_s": g.t_a_end,
            "estimated_missing_slave_samples": g.estimated_missing_slave_samples,
        } for g in correction.gaps],
    }
    estimate = {
        "drift_ppm": fit.drift_ppm,
        "offset_s": fit.offset_s,
        "n_sync_points_used": len(fit.inlier_points),
        "n_sync_points_rejected": len(fit.outlier_points),
        "source_mode": selected_mode,
        "fit_method": "RANSAC affine + OLS refit, segment-wise",
    }
    sync_points_report = {
        "all": [{"t_a_s": p.t_a, "t_b_s": p.t_b, "score": p.score,
                 "source": p.source} for p in points],
        "inliers": [{"t_a_s": p.t_a, "t_b_s": p.t_b, "score": p.score,
                     "source": p.source} for p in fit.inlier_points],
        "outliers": [{"t_a_s": p.t_a, "t_b_s": p.t_b, "score": p.score,
                      "source": p.source} for p in fit.outlier_points],
        "unmatched_reference_pulses_s": sorted(float(t) for t in unmatched_a),
        "unmatched_slave_pulses_s": sorted(float(t) for t in unmatched_b),
    }
    result = AlignmentResult(
        status="corrected", failure=None, warnings=warnings,
        uncertainties=uncertainties, steps=steps, estimate=estimate,
        residual_report=residual_report,
        timeline_map=correction.timeline_map, correction=correction,
        sync_points=sync_points_report, mode_used=selected_mode,
        external_time_anchor=external_time_anchor)
    for s in steps:
        log.info("step %s: %s", s.name, s.status, extra={"fields": s.detail})
    return result


def _uncorrected(code: str, message: str, steps, warnings, uncertainties,
                 external_time_anchor: bool, *, failure_code: str | None = None,
                 points=None, unmatched_a=None, unmatched_b=None
                 ) -> AlignmentResult:
    steps.append(StepRecord("fit.robust", "rejected", {"reason": message}))
    failure = {
        "code": failure_code or ("insufficient_evidence"
                                 if "evidence" in code or "sync point" in message
                                 or "span" in message else code),
        "message": message,
    }
    log.warning("alignment left uncorrected: %s", message)
    return AlignmentResult(
        status="uncorrected", failure=failure, warnings=warnings,
        uncertainties=uncertainties, steps=steps, estimate=None,
        residual_report=None, timeline_map=None, correction=None,
        sync_points={"all": [{"t_a_s": p.t_a, "t_b_s": p.t_b,
                              "score": p.score, "source": p.source}
                             for p in (points or [])],
                     "inliers": [], "outliers": [],
                     "unmatched_reference_pulses_s": sorted(unmatched_a or []),
                     "unmatched_slave_pulses_s": sorted(unmatched_b or [])},
        mode_used=None, external_time_anchor=external_time_anchor)
