"""FastAPI application: validation endpoints and job-state API.

Error semantics (JSON envelope ``{"error_code", "message", "detail"}``):

* 400 INVALID_REQUEST        malformed request body / empty content
* 404 JOB_NOT_FOUND          unknown job id
* 413 PAYLOAD_TOO_LARGE      document exceeds the configured cue/byte limits
* 422 (FastAPI default)      request fails schema validation
* 500 INTERNAL_ERROR         unexpected exception (never a silent success)

Document-level failures (bad input document, unfixable timing) are **not** HTTP
errors: they return 200 with an explicit ``status`` and ``failure_codes``.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import Settings
from ..jobs import JobService, JobStore
from ..logging_setup import configure_logging, get_logger
from ..services.pipeline import run_validation
from .schemas import JobSummary, ValidateRequest, ValidationResponse

log = get_logger("api")

_MAX_BODY_BYTES = 8 * 1024 * 1024


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Subtitle Guard",
        version=__version__,
        description="SRT/WebVTT restricted-subset subtitle validation and "
                    "minimum-displacement suggested repair.",
    )
    store = JobStore(settings.db_path)
    app.state.settings = settings
    app.state.job_service = JobService(store, settings)

    @app.exception_handler(HTTPException)
    async def _http_exc(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error_code": _error_code(exc.status_code),
                     "message": exc.detail, "detail": {}},
        )

    @app.get("/health")
    async def health() -> dict:
        import numpy as np
        import pydantic
        return {
            "status": "ok",
            "service": "subtitle-guard",
            "version": __version__,
            "python": os.sys.version.split()[0],
            "numpy": np.__version__,
            "fastapi": _fastapi_version(),
            "pydantic": pydantic.VERSION,
            "sqlite": _sqlite_version(store),
            "config": {
                "min_duration_ms": settings.min_duration_ms,
                "max_duration_ms": settings.max_duration_ms,
                "min_gap_ms": settings.min_gap_ms,
                "segment_boundaries_ms": list(settings.segment_boundaries_ms),
                "horizon_ms": settings.horizon_ms,
                "max_per_cue_shift_ms": settings.max_per_cue_shift_ms,
                "max_total_shift_ms": settings.max_total_shift_ms,
            },
        }

    @app.post("/api/v1/validate", response_model=ValidationResponse)
    async def validate(req: ValidateRequest) -> dict:
        if not req.content:
            raise HTTPException(400, "content must not be empty")
        data = req.content.encode("utf-8")
        if len(data) > _MAX_BODY_BYTES:
            raise HTTPException(413, f"document exceeds {_MAX_BODY_BYTES} bytes")
        result = run_validation(data, settings=settings, fmt=req.format)
        return _to_response_body(result)

    @app.post("/api/v1/jobs", status_code=201)
    async def create_job(req: ValidateRequest) -> dict:
        if not req.content:
            raise HTTPException(400, "content must not be empty")
        data = req.content.encode("utf-8")
        if len(data) > _MAX_BODY_BYTES:
            raise HTTPException(413, f"document exceeds {_MAX_BODY_BYTES} bytes")
        job_id = app.state.job_service.submit(data, req.format)
        return {"job_id": job_id,
                "url": f"/api/v1/jobs/{job_id}"}

    @app.get("/api/v1/jobs", response_model=list[JobSummary])
    async def list_jobs(limit: int = 50) -> list[dict]:
        limit = max(1, min(limit, 200))
        return [_without_result(r) for r in store.list(limit)]

    @app.get("/api/v1/jobs/{job_id}", response_model=JobSummary)
    async def get_job(job_id: str) -> dict:
        row = store.get(job_id)
        if row is None:
            raise HTTPException(404, f"job {job_id!r} not found")
        return row

    return app


def _to_response_body(result) -> dict:
    from .schemas import CueRepairModel, DiagnosticModel, RepairModel
    diagnostics = [
        DiagnosticModel(
            code=d.code,
            severity=d.severity.value,
            message=d.message,
            cue_index=d.cue_index,
            other_index=d.other_index,
            detail=d.detail,
        )
        for d in result.diagnostics
    ]
    repair = None
    if result.repair is not None:
        repair = RepairModel(
            status=result.repair["status"],
            message=result.repair["message"],
            total_shift_ms=result.repair["total_shift_ms"],
            max_shift_ms=result.repair["max_shift_ms"],
            budget_ms=result.repair["budget_ms"],
            cues=[CueRepairModel(**c) for c in result.repair["cues"]],
        )
    body = ValidationResponse(
        run_id=result.run_id,
        status=result.status,
        format=result.fmt,
        cue_count=result.cue_count,
        message=result.message,
        elapsed_ms=round(result.elapsed_ms, 3),
        failure_codes=result.failure_codes,
        diagnostics=diagnostics,
        repair=repair,
        repaired_document=result.repaired_document,
    )
    return body.model_dump()


def _without_result(row: dict) -> dict:
    row = dict(row)
    row["result"] = None
    row["repaired_document"] = None
    return row


def _error_code(status: int) -> str:
    return {
        400: "INVALID_REQUEST",
        404: "JOB_NOT_FOUND",
        413: "PAYLOAD_TOO_LARGE",
        500: "INTERNAL_ERROR",
    }.get(status, f"HTTP_{status}")


def _fastapi_version() -> str:
    import fastapi
    return fastapi.__version__


def _sqlite_version(store: JobStore) -> str:
    return store._conn.execute("SELECT sqlite_version()").fetchone()[0]


app = create_app()
