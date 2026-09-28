"""Job processing: orchestrates parser, compatibility, planner and
independent validation, persisting every state transition and progress
event.  Failures are categorised and recorded as failed jobs — never
returned as success."""
from __future__ import annotations

import logging
from fractions import Fraction
from pathlib import Path
from typing import Any

from app.config import Settings
from app.core.planner import build_concat_plan
from app.core.validate import validate_plan
from app.errors import FailureCategory, PlannerError
from app.jobs.store import JobStore
from app.logging_setup import get_logger
from app.media.parser import load_segment
from app.models import SegmentRequest

VALID_CONTAINERS = {"mp4-constrained"}


def resolve_path(raw: str, settings: Settings) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        candidate = Path(settings.fixture_dir) / raw
        if candidate.exists():
            p = candidate
    return p


def _build_requests(payload: dict[str, Any], settings: Settings) -> list[SegmentRequest]:
    reqs: list[SegmentRequest] = []
    for raw in payload["segments"]:
        trim_in = (Fraction(*raw["trim_in"]) if raw.get("trim_in")
                   else Fraction(0))
        trim_out = (Fraction(*raw["trim_out"]) if raw.get("trim_out")
                    else None)
        reqs.append(SegmentRequest(
            path=str(resolve_path(raw["path"], settings)),
            trim_in=trim_in, trim_out=trim_out))
    return reqs


def run_job(store: JobStore, settings: Settings, run_id: str,
            payload: dict[str, Any]) -> str:
    job_id = store.create_job(run_id, payload)
    log = get_logger("job", settings.log_dir, job_id=job_id)

    def fail(exc: PlannerError) -> str:
        log.log_step(logging.ERROR, "failed",
                     f"{exc.category.value}: {exc.detail}",
                     {"category": exc.category.value, **exc.context})
        store.fail_job(job_id, exc.category.value, exc.detail)
        return job_id

    try:
        container = payload.get("container") or settings.container
        if container not in VALID_CONTAINERS:
            return fail(PlannerError(
                FailureCategory.INPUT_ERROR,
                f"unsupported container {container!r}",
                {"allowed": sorted(VALID_CONTAINERS)}))
        requests = _build_requests(payload, settings)
        store.mark_analysing(job_id)
        log.log_step(logging.INFO, "parse", "loading segment descriptors", {
            "paths": [r.path for r in requests]})

        segments = []
        for req in requests:
            seg = load_segment(req.path)
            log.log_step(logging.INFO, "parse",
                         f"parsed {seg.name}",
                         {"path": req.path, "sha256": seg.sha256,
                          "streams": [s.stream_type for s in seg.streams]})
            segments.append(seg)

        store.add_event(job_id, "compat", "INFO", "running compatibility check", None)
        plan = build_concat_plan(segments, requests, container, log=log)
        store.add_event(job_id, "plan", "INFO",
                        f"plan built: decision={plan.decision}",
                        {"decision": plan.decision,
                         "reasons": plan.reasons})

        if plan.decision == "direct_concat":
            violations = validate_plan(plan, segments)
            log.log_step(logging.INFO, "validate",
                         f"independent validation: {len(violations)} violation(s)",
                         {"violations": [v.to_dict() for v in violations]})
            if violations:
                # keep the offending plan for inspection, then fail
                store.attach_plan(job_id, plan.to_dict())
                first = violations[0]
                return fail(PlannerError(
                    first.category,
                    "; ".join(v.detail for v in violations[:3]),
                    {"violations": [v.to_dict() for v in violations]}))

        store.complete_job(job_id, plan.decision, plan.to_dict(), plan.reasons)
        return job_id
    except PlannerError as exc:
        return fail(exc)
    except Exception as exc:  # noqa: BLE001 - boundary: categorise, never succeed
        log.log_step(logging.ERROR, "failed", f"unexpected error: {exc!r}",
                     {"exception": type(exc).__name__})
        store.fail_job(job_id, FailureCategory.INTERNAL_ERROR.value, repr(exc))
        return job_id
