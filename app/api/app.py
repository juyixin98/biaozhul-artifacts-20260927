"""FastAPI application: job submission, status, per-sample plan and an
independent validation endpoint."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.config import Settings, dependency_versions, get_settings
from app.core.validate import validate_plan
from app.errors import FailureCategory
from app.jobs.service import run_job
from app.jobs.store import JobStore
from app.logging_setup import get_run_id
from app.media.parser import load_segment
from app.models import ConcatPlan

RUN_ID = get_run_id()


class RationalSeconds(BaseModel):
    num: int
    den: int

    @field_validator("den")
    @classmethod
    def _den_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("denominator must be positive")
        return v


class SegmentIn(BaseModel):
    path: str
    trim_in: RationalSeconds | None = None
    trim_out: RationalSeconds | None = None


class JobRequest(BaseModel):
    segments: list[SegmentIn] = Field(min_length=1)
    container: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "segments": [
                {
                    "path": s.path,
                    "trim_in": [s.trim_in.num, s.trim_in.den]
                    if s.trim_in else None,
                    "trim_out": [s.trim_out.num, s.trim_out.den]
                    if s.trim_out else None,
                }
                for s in self.segments
            ],
        }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()
    store = JobStore(settings.db_path)
    app = FastAPI(
        title="Media Concat Sample-Boundary Planner",
        version=settings.app_version,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.run_id = RUN_ID

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "run_id": RUN_ID}

    @app.get("/version")
    def version() -> dict[str, Any]:
        return {"run_id": RUN_ID, "versions": dependency_versions(),
                "container_ruleset": settings.container_ruleset}

    @app.post("/jobs", status_code=201)
    def submit_job(req: JobRequest) -> dict[str, Any]:
        payload = req.to_payload()
        # fail fast on unreadable inputs: that is a client error, not a job
        for raw in payload["segments"]:
            from app.jobs.service import resolve_path
            p = resolve_path(raw["path"], settings)
            if not Path(p).is_file():
                raise HTTPException(
                    status_code=422,
                    detail={
                        "category": FailureCategory.INPUT_ERROR.value,
                        "detail": f"segment descriptor not found: {p}",
                    })
        job_id = run_job(store, settings, RUN_ID, payload)
        return store.get_job(job_id)

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.get("/jobs/{job_id}/plan")
    def get_plan(job_id: str) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if "plan" not in job:
            raise HTTPException(
                status_code=409,
                detail={"category": "NO_PLAN",
                        "job_status": job["status"],
                        "decision": job.get("decision"),
                        "error_category": job.get("error_category")})
        return job["plan"]

    @app.get("/jobs/{job_id}/events")
    def get_events(job_id: str) -> dict[str, Any]:
        if store.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {"job_id": job_id, "run_id": RUN_ID,
                "events": store.get_events(job_id)}

    @app.post("/jobs/{job_id}/validate")
    def validate_stored_job(job_id: str) -> dict[str, Any]:
        """Re-validate a stored plan independently."""
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if "plan" not in job:
            raise HTTPException(status_code=409, detail="job has no plan")
        plan = ConcatPlan.from_dict(job["plan"])
        from app.jobs.service import resolve_path
        segments = [load_segment(resolve_path(raw["path"], settings))
                    for raw in job["request"]["segments"]]
        violations = validate_plan(plan, segments)
        result = {"ok": not violations,
                  "violations": [v.to_dict() for v in violations]}
        store.add_event(job_id, "validate", "INFO",
                        f"re-validation ok={result['ok']}", result)
        return result

    @app.exception_handler(Exception)
    async def unhandled(request, exc):  # noqa: ANN001
        return JSONResponse(
            status_code=500,
            content={"category": FailureCategory.INTERNAL_ERROR.value,
                     "detail": repr(exc)},
        )

    return app


app = create_app()
