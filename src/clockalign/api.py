"""FastAPI application: job submission, status, artifacts and validation.

Every response carries ``X-Request-ID`` (echoing the client's header or a
generated one), the service version and the config file in use, so a user can
tie a result to the code and settings that produced it.
"""
from __future__ import annotations

import json
import uuid

import contextlib
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from . import __version__
from .config import Config, load_config
from .errors import MediaError
from .jobs import JobService
from .logging_setup import bind, configure_logging, get_logger
from .storage import JobStore
from .validation import validate_report
from .api_schemas import (AlignRequest, JobCreated, JobResponse, JobSummary,
                          ValidationRequest, ValidationResponse)

log = get_logger("api")

STATUS_TO_HTTP = {
    "succeeded": 200,
    "failed": 422,
    "rejected_insufficient_evidence": 200,
}


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    configure_logging()
    store = JobStore(cfg.storage.database_path)
    jobs = JobService(cfg, store)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        jobs.shutdown()
        store.close()

    app = FastAPI(
        title="clockalign",
        version=__version__,
        description="Two-track audio clock drift estimation and timeline "
                    "correction",
        lifespan=lifespan)
    app.state.config = cfg
    app.state.store = store
    app.state.jobs = jobs

    @app.middleware("http")
    async def identity_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        bind(request_id=request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Service-Version"] = __version__
        response.headers["X-Config-Source"] = str(cfg.source)
        return response

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "clockalign",
                "version": __version__, "config": str(cfg.source)}

    @app.post("/api/v1/align", response_model=JobCreated, status_code=202)
    async def align(req: AlignRequest,
                    x_request_id: str | None = Header(default=None)) -> dict:
        if not req.stereo_path and not (req.reference_path and req.slave_path):
            raise HTTPException(
                status_code=400, detail={
                    "code": "bad_request",
                    "message": "provide either stereo_path or both "
                               "reference_path and slave_path"})
        request_id = x_request_id or uuid.uuid4().hex
        bind(request_id=request_id)
        payload = req.model_dump()
        payload["request_id"] = request_id
        # Fail fast on missing/unreadable media: validate before queueing.
        from .media import load_pair
        try:
            load_pair(payload.get("reference_path"), payload.get("slave_path"),
                      stereo_path=payload.get("stereo_path"),
                      stereo_role=cfg.media.stereo_channel_role,
                      max_sample_rate=cfg.media.max_sample_rate,
                      sample_rate_mismatch_ppm_max=(
                          cfg.media.sample_rate_mismatch_ppm_max))
        except MediaError as exc:
            raise HTTPException(status_code=422, detail={
                "code": exc.code, "message": exc.message,
                "details": exc.details})
        job_id = store.create_job(request_id, payload)
        jobs.submit(job_id, payload)
        log.info("alignment accepted", extra={"fields": {"job_id": job_id}})
        return {"job_id": job_id, "request_id": request_id, "status": "queued"}

    @app.get("/api/v1/jobs", response_model=list[JobSummary])
    async def list_jobs(limit: int = 50) -> list[dict]:
        return store.list_jobs(limit=min(limit, 200))

    @app.get("/api/v1/jobs/{job_id}", response_model=JobResponse)
    async def get_job(job_id: str) -> dict:
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail={
                "code": "not_found", "message": f"unknown job {job_id}"})
        report = None
        if job.get("result_path"):
            with open(job["result_path"], "r", encoding="utf-8") as fh:
                report = json.load(fh)
        return {
            "job_id": job["job_id"], "request_id": job["request_id"],
            "status": job["status"], "version": __version__,
            "config_source": str(cfg.source),
            "submitted_at": job["submitted_at"],
            "updated_at": job["updated_at"], "failure": job.get("failure"),
            "events": job["events"], "report": report}

    @app.get("/api/v1/jobs/{job_id}/artifacts/{name}")
    async def get_artifact(job_id: str, name: str) -> FileResponse:
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail={"code": "not_found"})
        allowed = {"corrected_slave.wav", "timeline_map.json", "overlay.wav",
                   "report.json"}
        if name not in allowed:
            raise HTTPException(status_code=400, detail={
                "code": "bad_request", "message": f"unknown artifact {name}"})
        path = cfg.storage.artifacts_path / job_id / name
        if not path.exists():
            raise HTTPException(status_code=404, detail={
                "code": "not_found",
                "message": f"artifact {name} not present for this job "
                           f"(status={job['status']})"})
        media_type = ("audio/wav" if name.endswith(".wav")
                      else "application/json")
        return FileResponse(path, media_type=media_type, filename=name)

    @app.post("/api/v1/validate", response_model=ValidationResponse)
    async def validate(req: ValidationRequest) -> dict:
        job = store.get_job(req.job_id)
        if not job:
            raise HTTPException(status_code=404, detail={"code": "not_found"})
        if not job.get("result_path"):
            raise HTTPException(status_code=409, detail={
                "code": "job_not_finished",
                "message": f"job status is {job['status']}"})
        with open(job["result_path"], "r", encoding="utf-8") as fh:
            report = json.load(fh)
        truth = req.truth
        result = validate_report(report, cfg.validation, truth=truth)
        result.update({"job_id": req.job_id,
                       "request_id": job["request_id"]})
        return result

    return app


app = create_app()
