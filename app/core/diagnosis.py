"""Time & signal kernel — diagnostic detection.

Every check is independently computed and emits a specific failure code rather
than a generic pass/fail:

* ``negative_duration`` / ``zero_duration`` / ``too_short`` / ``too_long``
* ``same_start``        two cues beginning at the same timestamp
* ``overlap``           adjacent cues (in start-time order) with touching
                        intervals, including chained-overlap clusters
* ``crosses_boundary``  a cue spans a configured segment boundary
* ``starts_before_zero`` / ``ends_past_horizon``

Overlap detection is vectorized with NumPy: cues are stably sorted by
``(start, index)``; each adjacent pair is then classified in one sweep, so a
chain A⇢B⇢C surfaces as two concrete diagnostics (A,B) and (B,C).
"""
from __future__ import annotations

import numpy as np

from .models import Cue, Diagnostic, Severity

MIN_GAP_DEFAULT = 1


def diagnose(
    cues: list[Cue],
    *,
    min_duration_ms: int,
    max_duration_ms: int,
    segment_boundaries_ms: tuple[int, ...],
    horizon_ms: int,
    min_gap_ms: int = MIN_GAP_DEFAULT,
) -> list[Diagnostic]:
    diags: list[Diagnostic] = []
    if not cues:
        return diags

    starts = np.array([c.start_ms for c in cues], dtype=np.int64)
    ends = np.array([c.end_ms for c in cues], dtype=np.int64)
    durations = ends - starts

    # --- per-cue duration checks -------------------------------------------
    for cue, dur in zip(cues, durations.tolist()):
        diags.extend(_duration_diagnostics(cue, dur, min_duration_ms, max_duration_ms))
        if cue.start_ms < 0:
            diags.append(_diag("starts_before_zero", Severity.ERROR, cue, cue.start_ms))
        if cue.end_ms > horizon_ms:
            diags.append(
                Diagnostic(
                    code="ends_past_horizon",
                    severity=Severity.ERROR,
                    message=f"cue #{cue.index + 1} ends at {cue.end_ms}ms, past the "
                    f"content horizon {horizon_ms}ms",
                    cue_index=cue.index,
                    detail={"end_ms": cue.end_ms, "horizon_ms": horizon_ms},
                )
            )

    # --- overlap / same-start sweep over start-time order ------------------
    order = np.lexsort((np.arange(len(cues)), starts))  # stable by (start, index)
    ord_starts = starts[order]
    ord_ends = ends[order]
    ord_indices = order

    for pos in range(len(order) - 1):
        i = int(ord_indices[pos])
        j = int(ord_indices[pos + 1])
        s_i, e_i = int(ord_starts[pos]), int(ord_ends[pos])
        s_j = int(ord_starts[pos + 1])
        a, b = cues[i], cues[j]
        if s_i == s_j:
            diags.append(
                Diagnostic(
                    code="same_start",
                    severity=Severity.WARNING,
                    message=f"cues #{a.index + 1} and #{b.index + 1} share start "
                    f"timestamp {s_i}ms",
                    cue_index=a.index,
                    other_index=b.index,
                    detail={"start_ms": s_i},
                )
            )
        # overlap (or too-tight gap) — b starts before a has fully cleared.
        if s_j < e_i + min_gap_ms:
            conflict = max(0, e_i - s_j)
            diags.append(
                Diagnostic(
                    code="overlap",
                    severity=Severity.WARNING,
                    message=f"cue #{a.index + 1} [{s_i},{e_i}) overlaps cue "
                    f"#{b.index + 1} starting {s_j} by {conflict}ms",
                    cue_index=a.index,
                    other_index=b.index,
                    detail={
                        "prev_start_ms": s_i,
                        "prev_end_ms": e_i,
                        "next_start_ms": s_j,
                        "conflict_ms": int(conflict),
                        "required_gap_ms": min_gap_ms,
                    },
                )
            )

    # --- segment-boundary crossing -----------------------------------------
    boundaries = np.asarray(sorted(segment_boundaries_ms), dtype=np.int64)
    for cue in cues:
        hit = boundaries[(boundaries > cue.start_ms) & (boundaries < cue.end_ms)]
        for b in hit.tolist():
            diags.append(
                Diagnostic(
                    code="crosses_boundary",
                    severity=Severity.WARNING,
                    message=f"cue #{cue.index + 1} [{cue.start_ms},{cue.end_ms}) "
                    f"crosses segment boundary at {b}ms",
                    cue_index=cue.index,
                    detail={"boundary_ms": int(b)},
                )
            )

    # Stable, deterministic ordering of the full diagnostic list.
    diags.sort(key=lambda d: (d.cue_index if d.cue_index is not None else -1,
                              d.other_index if d.other_index is not None else -1,
                              d.code))
    return diags


def _duration_diagnostics(
    cue: Cue, dur: int, min_duration_ms: int, max_duration_ms: int
) -> list[Diagnostic]:
    if dur < 0:
        return [
            Diagnostic(
                code="negative_duration",
                severity=Severity.ERROR,
                message=f"cue #{cue.index + 1} ends ({cue.end_ms}ms) before it starts "
                f"({cue.start_ms}ms); duration {dur}ms",
                cue_index=cue.index,
                detail={"start_ms": cue.start_ms, "end_ms": cue.end_ms,
                        "duration_ms": dur},
            )
        ]
    if dur == 0:
        return [
            Diagnostic(
                code="zero_duration",
                severity=Severity.WARNING,
                message=f"cue #{cue.index + 1} is shown for 0ms at {cue.start_ms}ms",
                cue_index=cue.index,
                detail={"start_ms": cue.start_ms},
            )
        ]
    out: list[Diagnostic] = []
    if dur < min_duration_ms:
        out.append(
            Diagnostic(
                code="too_short",
                severity=Severity.WARNING,
                message=f"cue #{cue.index + 1} lasts {dur}ms (< minimum "
                f"{min_duration_ms}ms)",
                cue_index=cue.index,
                detail={"duration_ms": dur, "min_ms": min_duration_ms},
            )
        )
    if dur > max_duration_ms:
        out.append(
            Diagnostic(
                code="too_long",
                severity=Severity.WARNING,
                message=f"cue #{cue.index + 1} lasts {dur}ms (> maximum "
                f"{max_duration_ms}ms)",
                cue_index=cue.index,
                detail={"duration_ms": dur, "max_ms": max_duration_ms},
            )
        )
    return out


def _diag(code: str, severity: Severity, cue: Cue, value: int) -> Diagnostic:
    return Diagnostic(
        code=code,
        severity=severity,
        message=f"cue #{cue.index + 1} has illegal timestamp value {value}",
        cue_index=cue.index,
        detail={"value_ms": value},
    )
