"""HTTP routes: synchronous validation, async jobs, health.

Every response carries an ``X-Request-ID`` header (client-supplied or
generated).  Error bodies include it so a rejected upload can be correlated
with server logs.  User-supplied filenames are redacted in logs
(``redact_label``); raw analyzed bytes are never logged.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from ..core.analyzer import analyze
from ..core.diagnostics import redact_label
from ..jobs.manager import STATUS_DONE, STATUS_FAILED, JobManager
from .schemas import JobStatus, JobSubmitted

logger = logging.getLogger("mtsa.api")
router = APIRouter()


def _request_id(x_request_id: str | None) -> str:
    rid = (x_request_id or "").strip()
    return rid or uuid.uuid4().hex


async def _aread_limited(request: Request) -> bytes:
    settings = request.app.state.settings
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail={"error": "payload_too_large",
                        "max_bytes": settings.max_upload_bytes,
                        "request_id": getattr(request.state, "request_id", None)})
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    return {"status": "ok", "request_id": request.state.request_id}


@router.post("/api/v1/validate")
async def validate(request: Request,
                   x_request_id: str | None = Header(default=None)) -> JSONResponse:
    """Synchronously analyze a raw MPEG-TS body (content-type video/mp2t or
    application/octet-stream).  Returns the full report."""
    request.state.request_id = _request_id(x_request_id)
    data = await _aread_limited(request)
    if not data:
        raise HTTPException(
            status_code=400,
            detail={"error": "empty_body",
                    "request_id": request.state.request_id})
    logger.info("validate request_id=%s bytes=%d",
                request.state.request_id, len(data))
    report = analyze(data, request.state.request_id,
                     settings=request.app.state.settings)
    return JSONResponse(
        report.to_dict(),
        headers={"X-Request-ID": request.state.request_id})


@router.post("/api/v1/jobs", response_model=JobSubmitted, status_code=202)
async def create_job(request: Request,
                     x_request_id: str | None = Header(default=None)) -> JobSubmitted:
    """Queue an MPEG-TS analysis job from a raw body."""
    request.state.request_id = _request_id(x_request_id)
    data = await _aread_limited(request)
    if not data:
        raise HTTPException(
            status_code=400,
            detail={"error": "empty_body",
                    "request_id": request.state.request_id})
    manager: JobManager = request.app.state.jobs
    job_id = manager.submit(data, request.state.request_id)
    logger.info("job queued request_id=%s job_id=%s bytes=%d",
                request.state.request_id, job_id, len(data))
    return JobSubmitted(job_id=job_id,
                        request_id=request.state.request_id,
                        status="queued", input_bytes=len(data))


@router.post("/api/v1/jobs/upload", response_model=JobSubmitted, status_code=202)
async def upload_job(request: Request, file: UploadFile,
                     x_request_id: str | None = Header(default=None)
                     ) -> JobSubmitted:
    """Queue an analysis job from a multipart upload (field name ``file``)."""
    request.state.request_id = _request_id(x_request_id)
    settings = request.app.state.settings
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail={"error": "payload_too_large",
                    "max_bytes": settings.max_upload_bytes,
                    "request_id": request.state.request_id})
    if not data:
        raise HTTPException(
            status_code=400,
            detail={"error": "empty_file",
                    "request_id": request.state.request_id})
    manager: JobManager = request.app.state.jobs
    job_id = manager.submit(data, request.state.request_id,
                            input_name=redact_label(file.filename or ""))
    logger.info("upload job queued request_id=%s job_id=%s name=%s bytes=%d",
                request.state.request_id, job_id,
                redact_label(file.filename or ""), len(data))
    return JobSubmitted(job_id=job_id,
                        request_id=request.state.request_id,
                        status="queued", input_bytes=len(data))


@router.get("/api/v1/jobs/{job_id}", response_model=JobStatus)
async def get_job(job_id: str, request: Request,
                  x_request_id: str | None = Header(default=None)) -> JobStatus:
    request.state.request_id = _request_id(x_request_id)
    manager: JobManager = request.app.state.jobs
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "job_not_found", "job_id": job_id,
                    "request_id": request.state.request_id})
    return JobStatus(**job)


@router.get("/api/v1/jobs")
async def list_jobs(request: Request,
                    x_request_id: str | None = Header(default=None)
                    ) -> dict[str, Any]:
    request.state.request_id = _request_id(x_request_id)
    manager: JobManager = request.app.state.jobs
    return {"jobs": manager.list_jobs(),
            "request_id": request.state.request_id}
