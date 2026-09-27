"""Robust clock-model fitting.

The clock relation between the reference timeline ``tA`` and the slave
timeline ``tB`` is, between discontinuities, affine::

    tB = (1 + drift) * tA + offset

with ``drift`` in s/s (ppm in the report). Estimation is explicitly robust:

* a RANSAC line over the candidate sync points rejects *outlier sync points*
  (mis-fired detections, spurious device pulses);
* residuals of the accepted inliers are scanned for *jumps*; each jump is a
  discontinuity caused by a dropped/duplicated frame, and the timeline is split
  into independently valid segments there;
* with too few points, too short a span, too few inliers, or an implausible
  drift, no correction is produced (``status = insufficient_evidence``).

Evidence thresholds are never inferred from the data being judged -- they come
from configuration.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .errors import InsufficientEvidenceError


@dataclass(frozen=True)
class SyncPoint:
    t_a: float
    t_b: float
    score: float = 1.0
    source: str = "pulse"  # "pulse" | "correlation"


@dataclass(frozen=True)
class AffineModel:
    slope: float   # dtB/dtA = 1 + drift
    intercept: float

    def predict(self, t_a: np.ndarray | float) -> np.ndarray | float:
        return self.slope * t_a + self.intercept

    @property
    def drift_ppm(self) -> float:
        return (self.slope - 1.0) * 1e6


@dataclass(frozen=True)
class Segment:
    """One timeline interval in which the affine model holds."""

    index: int
    t_a_start: float
    t_a_end: float
    model: AffineModel
    rms_residual_s: float
    max_abs_residual_s: float
    n_points: int
    inlier_t_a: list[float] = field(default_factory=list)
    inlier_t_b: list[float] = field(default_factory=list)
    residual_s: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class FitResult:
    status: str                       # "ok" | "insufficient_evidence"
    global_model: AffineModel | None
    drift_ppm: float | None
    offset_s: float | None
    segments: tuple[Segment, ...]
    inlier_points: tuple[SyncPoint, ...]
    outlier_points: tuple[SyncPoint, ...]
    rms_residual_s: float | None
    max_abs_residual_s: float | None
    usable_t_a_range: tuple[float, float] | None
    reason: str | None = None


def _ols(t_a: np.ndarray, t_b: np.ndarray) -> AffineModel:
    slope, intercept = np.polyfit(t_a, t_b, 1)
    return AffineModel(slope=float(slope), intercept=float(intercept))


def robust_fit(points: list[SyncPoint], *, min_points: int, min_span_s: float,
               ransac_iterations: int, ransac_seed: int,
               inlier_threshold_s: float, min_inlier_fraction: float,
               min_inliers: int, max_drift_ppm: float,
               segment_min_points: int, segment_min_span_s: float,
               discontinuity_jump_s: float) -> FitResult:
    """Fit the clock model with explicit RANSAC outlier rejection.

    A single dominant line is required when the points carry no
    discontinuities; but genuine frame drops split the evidence into several
    valid lines. We therefore:

    1. run RANSAC to find a dominant line (>= ``min_inlier_fraction``);
    2. if no dominant line exists, search for *any* line with at least
       ``segment_min_points`` inliers, then recursively fit the remainder --
       multiple strong segments are evidence of a splice, not chaos;
    3. reject outright when fewer than ``min_points`` survive, span is too
       short, or drift is implausible.
    """
    pts = sorted(points, key=lambda p: p.t_a)
    if len(pts) < min_points:
        return FitResult(
            status="insufficient_evidence", global_model=None,
            drift_ppm=None, offset_s=None, segments=(),
            inlier_points=(), outlier_points=tuple(pts),
            rms_residual_s=None, max_abs_residual_s=None,
            usable_t_a_range=None,
            reason=f"only {len(pts)} sync point(s); need >= {min_points}")
    t_a = np.array([p.t_a for p in pts])
    t_b = np.array([p.t_b for p in pts])
    span = float(t_a[-1] - t_a[0])
    if span < min_span_s:
        return FitResult(
            status="insufficient_evidence", global_model=None,
            drift_ppm=None, offset_s=None, segments=(),
            inlier_points=(), outlier_points=tuple(pts),
            rms_residual_s=None, max_abs_residual_s=None,
            usable_t_a_range=None,
            reason=(f"sync points span {span:.3f}s; need >= {min_span_s:.3f}s"
                    " to estimate drift"))

    inliers = _ransac_line(t_a, t_b, iterations=ransac_iterations,
                           seed=ransac_seed, threshold_s=inlier_threshold_s,
                           min_inliers=min_inliers,
                           min_inlier_fraction=min_inlier_fraction)
    multi_segment_mode = False
    if inliers is None:
        # No single dominant line. Look for one or more partial-but-strong
        # lines; each must still clear the per-segment evidence bar.
        inliers = _ransac_line(t_a, t_b, iterations=ransac_iterations,
                               seed=ransac_seed, threshold_s=inlier_threshold_s,
                               min_inliers=segment_min_points,
                               min_inlier_fraction=0.0)
        multi_segment_mode = inliers is not None

    if inliers is None:
        return FitResult(
            status="insufficient_evidence", global_model=None,
            drift_ppm=None, offset_s=None, segments=(),
            inlier_points=(), outlier_points=tuple(pts),
            rms_residual_s=None, max_abs_residual_s=None,
            usable_t_a_range=None,
            reason="RANSAC could not find a consistent line; sync points are "
                   "too inconsistent for a trustworthy correction")

    inlier_mask = np.zeros(len(pts), dtype=bool)
    inlier_mask[inliers] = True
    in_pts = [pts[i] for i in inliers]
    out_pts = [pts[i] for i in np.flatnonzero(~inlier_mask)]

    # Collect the actual evidence lines. In the normal (single-dominant-line)
    # case there is one; in multi-segment mode the remainder is checked for
    # further strong lines (the far side of a frame splice has its own
    # offset-shifted line).
    line_groups: list[list[SyncPoint]] = [sorted(in_pts, key=lambda p: p.t_a)]
    if multi_segment_mode and len(out_pts) >= segment_min_points:
        extra_in, out_pts = _extract_extra_lines(
            out_pts, iterations=ransac_iterations, seed=ransac_seed,
            threshold_s=inlier_threshold_s, min_points=segment_min_points)
        line_groups.append(sorted(extra_in, key=lambda p: p.t_a))

    in_pts = sorted(
        (p for grp in line_groups for p in grp), key=lambda p: p.t_a)

    if len(in_pts) < min_points:
        return FitResult(
            status="insufficient_evidence", global_model=None,
            drift_ppm=None, offset_s=None, segments=(),
            inlier_points=tuple(in_pts), outlier_points=tuple(out_pts),
            rms_residual_s=None, max_abs_residual_s=None,
            usable_t_a_range=None,
            reason=(f"only {len(in_pts)} sync point(s) lie on a consistent "
                    f"clock model; need >= {min_points}"))

    ia = np.array([p.t_a for p in in_pts])
    ib = np.array([p.t_b for p in in_pts])

    # Build per-line segment models first, so a splice never contaminates the
    # global numbers.
    line_models: list[AffineModel] = []
    raw_groups: list[tuple[np.ndarray, np.ndarray]] = []
    for grp in line_groups:
        ga = np.array([p.t_a for p in grp])
        gb = np.array([p.t_b for p in grp])
        if multi_segment_mode and len(ga) >= segment_min_points:
            m = _ols(ga, gb)
        else:
            # Single-line case: discontinuity subdivision happens below once
            # the anchor model is known; here keep all inliers on one model.
            m = _ols(ga, gb)
        line_models.append(m)
        raw_groups.append((ga, gb))

    anchor = line_models[0]
    if abs(anchor.drift_ppm) > max_drift_ppm:
        return FitResult(
            status="insufficient_evidence", global_model=None,
            drift_ppm=None, offset_s=None, segments=(),
            inlier_points=tuple(in_pts), outlier_points=tuple(out_pts),
            rms_residual_s=None, max_abs_residual_s=None,
            usable_t_a_range=None,
            reason=(f"fitted drift {anchor.drift_ppm:.1f} ppm exceeds the "
                    f"plausible-clock limit {max_drift_ppm:.0f} ppm; likely a "
                    "format/pairing error, not clock drift"))

    # In single-line mode, subdivide the inlier group at residual jumps
    # (extra discontinuities a dominant RANSAC line absorbed). In
    # multi-segment mode each discovered line is already one segment.
    if multi_segment_mode:
        groups = [(ga, gb, m) for (ga, gb), m in zip(raw_groups, line_models)]
    else:
        split = _split_segments(ia, ib, jump_s=discontinuity_jump_s,
                                min_points=segment_min_points,
                                min_span_s=segment_min_span_s)
        groups = [(ga, gb, anchor) for ga, gb in split]

    segments: list[Segment] = []
    for idx, (ga, gb, base) in enumerate(groups):
        model = _ols(ga, gb) if len(ga) >= 2 else base
        res = gb - model.predict(ga)
        segments.append(Segment(
            index=idx, t_a_start=float(ga[0]), t_a_end=float(ga[-1]),
            model=model, rms_residual_s=float(np.sqrt(np.mean(res ** 2))),
            max_abs_residual_s=float(np.max(np.abs(res))), n_points=len(ga),
            inlier_t_a=[float(v) for v in ga],
            inlier_t_b=[float(v) for v in gb],
            residual_s=[float(v) for v in res]))

    if not segments:
        return FitResult(
            status="insufficient_evidence", global_model=None,
            drift_ppm=None, offset_s=None, segments=(),
            inlier_points=tuple(in_pts), outlier_points=tuple(out_pts),
            rms_residual_s=None, max_abs_residual_s=None,
            usable_t_a_range=None,
            reason="no segment retained enough evidence after discontinuity "
                   "splitting")

    global_model = segments[0].model
    res_all = np.concatenate([
        gb - m.predict(ga) for (ga, gb, m) in groups])
    return FitResult(
        status="ok", global_model=global_model,
        drift_ppm=global_model.drift_ppm,
        offset_s=global_model.intercept, segments=tuple(segments),
        inlier_points=tuple(in_pts), outlier_points=tuple(out_pts),
        rms_residual_s=float(np.sqrt(np.mean(res_all ** 2))),
        max_abs_residual_s=float(np.max(np.abs(res_all))),
        usable_t_a_range=(float(ia[0]), float(ia[-1])))


def _extract_extra_lines(remainder: list[SyncPoint], *, iterations: int,
                         seed: int, threshold_s: float, min_points: int
                         ) -> tuple[list[SyncPoint], list[SyncPoint]]:
    """Pull any further strong affine subsets out of residual points.

    Used after a frame-drop splice: points on the far side of the splice form
    their own valid (offset-shifted) line. Points that fit no line are kept as
    outliers.
    """
    accepted: list[SyncPoint] = []
    leftover = list(remainder)
    while len(leftover) >= min_points:
        t_a = np.array([p.t_a for p in leftover])
        t_b = np.array([p.t_b for p in leftover])
        inl = _ransac_line(t_a, t_b, iterations=iterations, seed=seed,
                           threshold_s=threshold_s, min_inliers=min_points,
                           min_inlier_fraction=0.0)
        if inl is None:
            break
        mask = np.zeros(len(leftover), dtype=bool)
        mask[inl] = True
        accepted.extend(leftover[i] for i in inl)
        leftover = [p for k, p in enumerate(leftover) if not mask[k]]
    return accepted, leftover


def _ransac_line(t_a: np.ndarray, t_b: np.ndarray, *, iterations: int,
                 seed: int, threshold_s: float, min_inliers: int,
                 min_inlier_fraction: float) -> np.ndarray | None:
    """Return indices of inliers of the best line, or None.

    Iterations sample pairs of points deterministically. Scoring is MSAC-style:
    every inlier contributes ``1 - (residual/threshold)^2``, so a line with a
    few *tight* inliers beats a longer line whose extra inliers sit near the
    threshold. That matters at a frame splice, where a spurious line drawn
    across both segments can nominally touch many points but fits none tightly.
    """
    n = len(t_a)
    rng = np.random.default_rng(seed)
    best_inliers: np.ndarray | None = None
    best_cost: float = -np.inf
    pair_seen: set[tuple[int, int]] = set()
    for _ in range(iterations):
        i, j = int(rng.integers(n)), int(rng.integers(n))
        if i == j:
            continue
        key = (i, j) if i < j else (j, i)
        if key in pair_seen or abs(t_a[i] - t_a[j]) < 1e-12:
            continue
        pair_seen.add(key)
        slope = (t_b[j] - t_b[i]) / (t_a[j] - t_a[i])
        intercept = t_b[i] - slope * t_a[i]
        res = np.abs(t_b - (slope * t_a + intercept))
        inl = np.flatnonzero(res <= threshold_s)
        if inl.size < min_inliers:
            continue
        cost = float(np.sum(1.0 - (res[inl] / threshold_s) ** 2))
        if cost > best_cost:
            best_cost = cost
            best_inliers = inl
    if best_inliers is None:
        return None
    if best_inliers.size / n < min_inlier_fraction:
        return None
    return best_inliers


def _split_segments(t_a: np.ndarray, t_b: np.ndarray, *, jump_s: float,
                    min_points: int, min_span_s: float
                    ) -> list[tuple[np.ndarray, np.ndarray]]:
    """Split inliers at residual discontinuities (dropped/duplicated frames).

    A running local line is unnecessary: fit the global OLS line, take its
    residuals, and cut wherever the residual jumps by more than ``jump_s``
    between adjacent sorted points. Groups shorter than ``min_points`` or
    ``min_span_s`` are dropped from the usable set (and reported elsewhere as
    coverage gaps by the resampler).
    """
    model = _ols(t_a, t_b)
    res = t_b - model.predict(t_a)
    groups: list[tuple[np.ndarray, np.ndarray]] = []
    start = 0
    for k in range(1, len(t_a)):
        if abs(res[k] - res[k - 1]) > jump_s:
            groups.append((t_a[start:k], t_b[start:k]))
            start = k
    groups.append((t_a[start:], t_b[start:]))
    return [(ga, gb) for ga, gb in groups
            if len(ga) >= min_points and (ga[-1] - ga[0]) >= min_span_s]


def require_fit(result: FitResult) -> FitResult:
    """Raise :class:`InsufficientEvidenceError` for a rejected fit."""
    if result.status != "ok":
        raise InsufficientEvidenceError(result.reason or "insufficient evidence")
    return result
