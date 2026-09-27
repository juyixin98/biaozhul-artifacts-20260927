"""Orchestration pipeline: parse -> diagnose -> solve -> render.

This module is deliberately format/transport agnostic so the API layer and the
local demo script share one code path. It never collapses exception/unknown
states into success: parse failures and solver failures are reported with
explicit statuses and concrete failure codes.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from ..config import Settings
from ..core.diagnosis import diagnose
from ..core.models import Diagnostic, Severity
from ..core.solver import solve
from ..logging_setup import get_logger
from ..media import parse_bytes, render_document
from ..media.writer import RenderedCue

log = get_logger("pipeline")


@dataclass
class PipelineResult:
    run_id: str
    status: str                       # parse_failed|clean|repaired|infeasible_bounds
                                      # |budget_exceeded|solver_too_large
    fmt: str
    cue_count: int
    diagnostics: list[Diagnostic] = field(default_factory=list)
    repair: dict | None = None
    repaired_document: str | None = None
    failure_codes: list[str] = field(default_factory=list)
    message: str = ""
    elapsed_ms: float = 0.0

    def is_success(self) -> bool:
        return self.status in {"clean", "repaired"}


def run_validation(
    data: bytes,
    *,
    settings: Settings,
    fmt: str | None = None,
    run_id: str | None = None,
) -> PipelineResult:
    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
    t0 = time.perf_counter()
    log.info("[%s] validation start: %d bytes, fmt=%s", run_id, len(data), fmt)

    parsed = parse_bytes(data, fmt, max_cues=settings.max_cues)

    parse_errors = [d for d in parsed.diagnostics if d.severity == Severity.ERROR]
    fatal_codes = sorted({d.code for d in parse_errors})
    if parse_errors:
        # A parse error is fatal even if some cues were recovered: we never
        # repair a partially understood document silently.
        elapsed = (time.perf_counter() - t0) * 1000
        log.info("[%s] parse failed with %d error(s): %s",
                 run_id, len(parse_errors), fatal_codes)
        return PipelineResult(
            run_id=run_id,
            status="parse_failed",
            fmt=parsed.fmt,
            cue_count=len(parsed.cues),
            diagnostics=list(parsed.diagnostics),
            failure_codes=fatal_codes,
            message="document failed strict parsing; no repair was attempted",
            elapsed_ms=elapsed,
        )

    timing_diags = diagnose(
        parsed.cues,
        min_duration_ms=settings.min_duration_ms,
        max_duration_ms=settings.max_duration_ms,
        segment_boundaries_ms=settings.segment_boundaries_ms,
        horizon_ms=settings.horizon_ms,
        min_gap_ms=settings.min_gap_ms,
    )
    all_diags = list(parsed.diagnostics) + timing_diags

    codes_by_cue: dict[int, set[str]] = {}
    for d in all_diags:
        if d.cue_index is not None:
            codes_by_cue.setdefault(d.cue_index, set()).add(d.code)

    if not timing_diags:
        elapsed = (time.perf_counter() - t0) * 1000
        log.info("[%s] clean: %d cues, 0 timing diagnostics", run_id, len(parsed.cues))
        return PipelineResult(
            run_id=run_id,
            status="clean",
            fmt=parsed.fmt,
            cue_count=len(parsed.cues),
            diagnostics=all_diags,
            message=f"{len(parsed.cues)} cue(s), no timing problems found",
            elapsed_ms=elapsed,
        )

    plan = solve(
        parsed.cues,
        codes_by_cue,
        min_duration_ms=settings.min_duration_ms,
        max_duration_ms=settings.max_duration_ms,
        min_gap_ms=settings.min_gap_ms,
        segment_boundaries_ms=settings.segment_boundaries_ms,
        horizon_ms=settings.horizon_ms,
        max_per_cue_shift_ms=settings.max_per_cue_shift_ms,
        max_total_shift_ms=settings.max_total_shift_ms,
        run_id=run_id,
    )

    repaired_document = None
    repair_payload: dict | None = None
    if plan.status == "repaired":
        by_idx = {r.cue_index: r for r in plan.cues}
        rows: list[RenderedCue] = []
        for c in parsed.cues:  # original document order; no cue dropped
            r = by_idx[c.index]
            rows.append(RenderedCue(
                start_ms=r.repaired_start_ms,
                end_ms=r.repaired_end_ms,
                raw_lines=c.raw_lines,          # text preserved verbatim
                identifier=c.identifier,
            ))
        repaired_document = render_document(
            parsed.fmt, rows,
            had_bom=parsed.had_bom, header=parsed.header,
        )
        repair_payload = {
            "status": plan.status,
            "message": plan.message,
            "total_shift_ms": plan.total_shift_ms,
            "max_shift_ms": plan.max_shift_ms,
            "budget_ms": plan.budget_ms,
            "cues": [
                {
                    "cue_index": r.cue_index,
                    "original_start_ms": r.original_start_ms,
                    "original_end_ms": r.original_end_ms,
                    "repaired_start_ms": r.repaired_start_ms,
                    "repaired_end_ms": r.repaired_end_ms,
                    "shift_ms": r.shift_ms,
                    "action": r.action,
                    "reasons": list(r.reasons),
                }
                for r in plan.cues
            ],
        }

    elapsed = (time.perf_counter() - t0) * 1000
    result = PipelineResult(
        run_id=run_id,
        status=plan.status,
        fmt=parsed.fmt,
        cue_count=len(parsed.cues),
        diagnostics=all_diags,
        repair=repair_payload,
        repaired_document=repaired_document,
        failure_codes=[] if plan.status == "repaired" else [plan.status],
        message=plan.message,
        elapsed_ms=elapsed,
    )
    log.info("[%s] finished status=%s in %.1fms: %s",
             run_id, result.status, elapsed, result.message)
    return result
