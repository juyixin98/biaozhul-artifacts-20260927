"""Precise localization of frame-drop/duplicate discontinuities.

Robust fitting finds *that* a discontinuity exists between two adjacent
accepted sync points, but the cut can sit anywhere in the (possibly long)
interval between them. This module refines its position using the same
"known correlated snippet" evidence as rule 1 allows: a short reference window
is matched against the slave at both the pre-cut and post-cut clock models.
Before the cut the pre model correlates best, after it the post model does;
the zero crossing of their score difference localizes the splice to a few
milliseconds.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .timefit import Segment


@dataclass(frozen=True)
class Cut:
    """A discontinuity located on the reference (reality) timeline."""

    t_a: float                       # reference time of the splice start
    jump_b: float                    # post intercept minus pre intercept (s)
    kind: str                        # "frame_drop" | "frame_duplicate"
    localization: str                # "content_scan" | "midpoint_fallback"
    scan_score_margin: float | None  # post minus pre score confidence


def _ncc_at(x_win: np.ndarray, y: np.ndarray, center: float) -> float:
    """Normalized correlation of ``x_win`` centred at fractional index in y."""
    w = x_win.size
    start = int(round(center - w / 2.0))
    if start < 0 or start + w > y.size:
        return -2.0
    seg = y[start: start + w]
    denom = float(np.sqrt(np.sum(x_win * x_win) * np.sum(seg * seg)))
    if denom < 1e-12:
        return -2.0
    return float(np.sum(x_win * seg) / denom)


def locate_cut(reference: np.ndarray, slave: np.ndarray, sample_rate: int,
               seg_before: Segment, seg_after: Segment, *,
               window_s: float = 0.1, step_s: float = 0.0025
               ) -> Cut:
    """Locate the splice bracketed by two fitted segments.

    Falls back to the midpoint between the two flanking sync points when the
    content scan does not show a clear transition; the fallback is reported
    explicitly so it never masquerades as a confident result.
    """
    a = np.asarray(reference, dtype=np.float64)
    b = np.asarray(slave, dtype=np.float64)
    w = int(round(window_s * sample_rate))
    step = max(1, int(round(step_s * sample_rate)))

    lo = seg_before.t_a_end
    hi = seg_after.t_a_start
    midpoint = 0.5 * (lo + hi)
    jump_b = seg_after.model.intercept - seg_before.model.intercept
    kind = "frame_drop" if jump_b < 0 else "frame_duplicate"
    if hi - lo < 2.0 * window_s or w < 8:
        return Cut(t_a=midpoint, jump_b=jump_b, kind=kind,
                   localization="midpoint_fallback", scan_score_margin=None)

    centers_t: list[float] = []
    diffs: list[float] = []
    margin = window_s / 2.0
    start_idx = int(round((lo + margin) * sample_rate))
    end_idx = int(round((hi - margin) * sample_rate))
    for c in range(start_idx, end_idx + 1, step):
        t = c / sample_rate
        if c - w // 2 < 0 or c + w // 2 + 1 > a.size:
            continue
        x_win = a[c - w // 2: c - w // 2 + w]
        if float(np.sum(x_win * x_win)) < 1e-10:
            continue
        t_b_pre = seg_before.model.predict(t)
        t_b_post = seg_after.model.predict(t)
        s_pre = _ncc_at(x_win, b, t_b_pre * sample_rate - 0.5)
        s_post = _ncc_at(x_win, b, t_b_post * sample_rate - 0.5)
        centers_t.append(t)
        diffs.append(s_post - s_pre)

    if len(centers_t) < 3:
        return Cut(t_a=midpoint, jump_b=jump_b, kind=kind,
                   localization="midpoint_fallback", scan_score_margin=None)

    ct = np.array(centers_t)
    dd = np.array(diffs)
    # Require a real transition: negative on the pre side, positive post side.
    pre_side = ct < midpoint
    post_side = ~pre_side
    margin_score = (float(np.mean(dd[post_side])) - float(np.mean(dd[pre_side]))) \
        if pre_side.any() and post_side.any() else 0.0
    crossing = None
    for k in range(1, len(dd)):
        if dd[k - 1] <= 0.0 < dd[k]:
            # Linear interpolation of the zero crossing.
            frac = -dd[k - 1] / (dd[k] - dd[k - 1])
            crossing = ct[k - 1] + frac * (ct[k] - ct[k - 1])
            break
    if crossing is None or margin_score < 0.2:
        return Cut(t_a=midpoint, jump_b=jump_b, kind=kind,
                   localization="midpoint_fallback", scan_score_margin=margin_score)
    return Cut(t_a=float(crossing), jump_b=jump_b, kind=kind,
               localization="content_scan", scan_score_margin=margin_score)


def locate_all_cuts(reference: np.ndarray, slave: np.ndarray, sample_rate: int,
                    segments: tuple[Segment, ...], **kwargs) -> list[Cut]:
    ordered = sorted(segments, key=lambda s: s.t_a_start)
    cuts: list[Cut] = []
    for k in range(1, len(ordered)):
        cuts.append(locate_cut(reference, slave, sample_rate,
                               ordered[k - 1], ordered[k], **kwargs))
    return cuts
