"""Timeline correction by resampling -- output separately from metadata.

Two distinct artifacts are produced:

1. ``corrected_audio``: slave samples resampled onto the reference *time*
   axis. Each fitted segment is warped with its own affine map; a dropped
   frame is emitted as an explicit zero gap (the missing audio is never
   invented), a duplicated frame as an overlap where the later model wins.
   Samples outside the observed evidence interval are not extrapolated.

2. ``timeline_map``: a pure-data description of the time mapping
   (``slave_time -> reference_time`` per segment, gaps, coverage) suitable for
   aligning metadata/labels without touching audio bytes.

The resampler does not import the fixture generator or any pipeline code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .discontinuity import Cut
from .timefit import FitResult, Segment


@dataclass(frozen=True)
class Gap:
    t_a_start: float
    t_a_end: float
    estimated_missing_slave_samples: float
    kind: str  # "frame_drop" | "frame_duplicate" | "coverage"


@dataclass(frozen=True)
class Correction:
    corrected_audio: np.ndarray          # float32 on the reference grid
    sample_rate: int
    timeline_map: dict
    gaps: tuple[Gap, ...]
    covered_t_a_range: tuple[float, float]
    output_t_a_range: tuple[float, float]


def _resample_segment(slave: np.ndarray, sample_rate: int, seg: Segment,
                      *, edge_guard_s: float, a_start: int, a_end: int
                      ) -> np.ndarray:
    """Warp slave samples for segment ``seg`` onto reference indices.

    tB = slope*tA + intercept -> for each reference sample centre tA,
    read slave at fractional index tB*fs via linear interpolation.
    """
    idx_a = np.arange(a_start, a_end, dtype=np.float64)
    t_a = (idx_a + 0.5) / sample_rate
    t_b = seg.model.predict(t_a)
    x = t_b * sample_rate - 0.5
    x0 = np.floor(x).astype(np.int64)
    frac = x - x0
    n = slave.size
    lo = np.clip(x0, 0, n - 1)
    hi = np.clip(x0 + 1, 0, n - 1)
    out = slave[lo] * (1.0 - frac) + slave[hi] * frac
    valid = ((t_b >= -edge_guard_s) & (t_b <= n / sample_rate + edge_guard_s)
             & (t_a >= seg.t_a_start - edge_guard_s)
             & (t_a <= seg.t_a_end + edge_guard_s))
    return np.where(valid, out, 0.0).astype(np.float32)


def correct_track(slave: np.ndarray, sample_rate: int, fit: FitResult,
                  *, reference_length: int | None = None,
                  edge_guard_s: float = 0.005,
                  cuts: Sequence[Cut] | None = None) -> Correction:
    """Build the corrected slave track and the separate timeline metadata.

    ``cuts`` optionally pins each discontinuity to a localized reference time;
    without it, cuts fall back to the midpoint between flanking sync points
    and are labelled as such in the metadata.
    """
    if fit.status != "ok" or fit.global_model is None:
        raise ValueError("cannot correct: fit result is not usable")

    out_len = int(reference_length or slave.size)
    corrected = np.zeros(out_len, dtype=np.float32)
    ordered = sorted(fit.segments, key=lambda s: s.t_a_start)
    out_end_t = out_len / sample_rate

    # Segment boundaries on the reference grid: adjacent segments meet at
    # the localized cut time; outer edges are bounded by the evidence range.
    bounds: list[float] = []
    for k, seg in enumerate(ordered):
        if k == 0:
            bounds.append(max(0.0, seg.t_a_start - edge_guard_s))
        if k + 1 < len(ordered):
            cut = cuts[k] if cuts and len(cuts) > k else None
            bounds.append(cut.t_a if cut is not None
                          else 0.5 * (seg.t_a_end + ordered[k + 1].t_a_start))
        else:
            bounds.append(min(out_end_t, seg.t_a_end + edge_guard_s))

    seg_entries: list[dict] = []
    for k, seg in enumerate(ordered):
        lo_t, hi_t = bounds[k], bounds[k + 1]
        a_start = max(0, int(np.floor(lo_t * sample_rate)))
        a_end = min(out_len, int(np.ceil(hi_t * sample_rate)))
        if a_end > a_start:
            # Later segments overwrite earlier ones across duplicated frames.
            corrected[a_start:a_end] = _resample_segment(
                slave, sample_rate, seg, edge_guard_s=edge_guard_s,
                a_start=a_start, a_end=a_end)
        seg_entries.append({
            "segment_index": seg.index,
            "reference_time": {"start_s": lo_t, "end_s": hi_t},
            "slave_time": {
                "start_s": float(seg.model.predict(lo_t)),
                "end_s": float(seg.model.predict(hi_t)),
            },
            "mapping": {
                "model": "t_slave = slope * t_reference + intercept",
                "slope": seg.model.slope,
                "intercept_s": seg.model.intercept,
                "drift_ppm": seg.model.drift_ppm,
            },
            "fit_evidence": {
                "reference_span_s": [seg.t_a_start, seg.t_a_end],
                "n_sync_points": seg.n_points,
                "rms_residual_s": seg.rms_residual_s,
                "max_abs_residual_s": seg.max_abs_residual_s,
                "sync_points_reference_s": seg.inlier_t_a,
                "residual_s": seg.residual_s,
            },
        })

    gaps: list[Gap] = []
    for k in range(1, len(ordered)):
        cut = cuts[k - 1] if cuts and len(cuts) >= k else None
        t_cut = bounds[k]
        jump_b = (ordered[k].model.intercept - ordered[k - 1].model.intercept
                  if cut is None else cut.jump_b)
        kind = ("frame_drop" if jump_b < 0 else "frame_duplicate") \
            if cut is None else cut.kind
        width_a = abs(jump_b) / max(abs(ordered[k].model.slope), 1e-9)
        if kind == "frame_drop":
            g = Gap(t_a_start=t_cut, t_a_end=t_cut + width_a,
                    estimated_missing_slave_samples=abs(jump_b) * sample_rate,
                    kind=kind)
        else:  # duplicate: overlapping coverage, not a hole in the output
            g = Gap(t_a_start=t_cut - width_a, t_a_end=t_cut,
                    estimated_missing_slave_samples=-abs(jump_b) * sample_rate,
                    kind=kind)
        gaps.append(g)

    evidence_lo = min(s.t_a_start for s in ordered)
    evidence_hi = max(s.t_a_end for s in ordered)
    if evidence_lo > 0:
        gaps.append(Gap(t_a_start=0.0, t_a_end=float(evidence_lo),
                        estimated_missing_slave_samples=0.0, kind="coverage"))
    if evidence_hi < out_end_t:
        gaps.append(Gap(t_a_start=float(evidence_hi), t_a_end=out_end_t,
                        estimated_missing_slave_samples=0.0, kind="coverage"))

    timeline_map = {
        "time_basis": "track-relative; pulse/correlation alignments establish "
                      "relative time only and are not proof of absolute "
                      "wall-clock time without an external anchor",
        "reference_clock": {
            "sample_rate_hz": sample_rate,
            "grid": "sample centres at (index + 0.5) / sample_rate",
        },
        "global_model": None if fit.global_model is None else {
            "slope": fit.global_model.slope,
            "intercept_s": fit.global_model.intercept,
            "drift_ppm": fit.global_model.drift_ppm,
        },
        "segments": seg_entries,
        "gaps": [{
            "reference_time_start_s": g.t_a_start,
            "reference_time_end_s": g.t_a_end,
            "estimated_missing_slave_samples": g.estimated_missing_slave_samples,
            "kind": g.kind,
        } for g in sorted(gaps, key=lambda x: x.t_a_start)],
        "cuts": [{
            "reference_time_s": (cuts[k - 1].t_a if cuts and len(cuts) >= k
                                 else bounds[k]),
            "localization": (cuts[k - 1].localization if cuts and len(cuts) >= k
                             else "midpoint_fallback"),
            "scan_score_margin": (cuts[k - 1].scan_score_margin
                                  if cuts and len(cuts) >= k else None),
        } for k in range(1, len(ordered))],
        "usable_reference_range_s": [fit.usable_t_a_range[0],
                                     fit.usable_t_a_range[1]],
    }
    return Correction(
        corrected_audio=corrected, sample_rate=sample_rate,
        timeline_map=timeline_map,
        gaps=tuple(sorted(gaps, key=lambda x: x.t_a_start)),
        covered_t_a_range=(float(evidence_lo), float(evidence_hi)),
        output_t_a_range=(0.0, out_end_t))
