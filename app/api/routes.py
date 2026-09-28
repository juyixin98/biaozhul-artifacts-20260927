"""HTTP routes: async job submission/status and a synchronous validate API."""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, File, Header, HTTPException, Query, Request, UploadFile

from ..jobs.manager import analyze_bytes
from .schemas import (
    HealthResponse,
    JobCreated,
    JobStatusResponse,
    JobSummary,
    ValidateResponse,
)

MAX_VALIDATE_BYTES_HEADER = 64 * 1024 * 1024


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health", response_model=HealthResponse, tags=["meta"])
    def health() -> HealthResponse:
        return HealthResponse()

    # ------------------------------------------------------------------ jobs
    @router.post("/jobs", response_model=JobCreated, status_code=202, tags=["jobs"])
    async def create_job(
        request: Request, file: UploadFile = File(..., description="raw .ts bytes")
    ) -> JobCreated:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="empty upload: no bytes received")
        if len(data) > request.app.state.settings.max_input_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"input too large: {len(data)} > "
                f"{request.app.state.settings.max_input_bytes}",
            )
        manager = request.app.state.manager
        job_id = manager.submit(data, input_name=file.filename)
        return JobCreated(job_id=job_id, status="queued")

    @router.get("/jobs", response_model=list[JobSummary], tags=["jobs"])
    def list_jobs(request: Request, limit: int = Query(50, ge=1, le=200)):
        return request.app.state.store.list_jobs(limit=limit)

    @router.get(
        "/jobs/{job_id}", response_model=JobStatusResponse, tags=["jobs"]
    )
    def get_job(request: Request, job_id: str) -> JobStatusResponse:
        row = request.app.state.store.get_job(job_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
        return JobStatusResponse(**row)

    @router.get("/jobs/{job_id}/report", tags=["jobs"])
    def get_report(request: Request, job_id: str):
        store = request.app.state.store
        row = store.get_job(job_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
        report = store.get_report(job_id)
        if report is None:
            return {
                "job_id": job_id,
                "status": row["status"],
                "detail": "report not available yet"
                if row["status"] in ("queued", "running")
                else "job failed before report",
            }
        return report

    @router.get("/jobs/{job_id}/events", tags=["jobs"])
    def get_events(
        request: Request,
        job_id: str,
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        store = request.app.state.store
        if store.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
        return {
            "job_id": job_id,
            "events": store.get_events(job_id, limit=limit, offset=offset),
        }

    # -------------------------------------------------------------- validate
    @router.post(
        "/validate", response_model=ValidateResponse, tags=["validation"]
    )
    async def validate(
        request: Request,
        file: UploadFile = File(..., description="raw .ts bytes to validate"),
        x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    ) -> ValidateResponse:
        """Synchronous one-shot validation with an explicit tri-state verdict."""
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="empty upload: no bytes received")
        if len(data) > request.app.state.settings.max_input_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"input too large: {len(data)} > "
                f"{request.app.state.settings.max_input_bytes}",
            )

        record_id = x_request_id or uuid.uuid4().hex
        report, result = analyze_bytes(
            data, request.app.state.settings, record_id=record_id
        )
        report["input"] = {
            "name": file.filename,
            "size": len(data),
        }
        counts = result.diagnostics.counts_by_severity()
        packets = result.framing.packets_parsed

        if result.framing.fatal is not None:
            verdict = "indeterminate"
            reason = (
                "sync recovery never locked: the buffer cannot be parsed as a"
                " 188-byte TS stream within the bounded scan window"
            )
        elif packets == 0:
            verdict = "indeterminate"
            reason = "no complete 188-byte packet present; nothing judgeable"
        elif counts["error"] > 0:
            verdict = "rejected"
            reason = (
                f"{counts['error']} error-level diagnostic(s) recorded"
                " (see events with codes and offsets for the rejection causes)"
            )
        else:
            verdict = "accepted"
            reason = "parsed packets satisfied all enabled transport checks"
            if counts["warning"]:
                reason += f"; {counts['warning']} warning(s) noted"

        return ValidateResponse(
            record_id=record_id,
            verdict=verdict,
            reason=reason,
            fatal=result.framing.fatal,
            severity_counts=counts,
            error_count=counts["error"],
            warning_count=counts["warning"],
            packets_parsed=packets,
            bytes_skipped=result.framing.bytes_skipped,
            report=report,
        )

    return router
