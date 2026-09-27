"""FastAPI application: submit correction jobs, inspect state and reports.

Every request carries a request id (X-Request-ID header, or a generated one)
that is echoed in the response headers, stored on the job row, and attached
to every pipeline log line — so a report, a job row and the log stream can
always be tied together.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import PIPELINE_VERSION, __version__
from ..config import AppConfig, load_config
from ..jobs.models import JobRecord
from ..jobs.store import JobStore
from ..logging_utils import get_logger, log_step
from ..media.metadata import MetadataError, load_metadata
from ..media.wav_io import MediaDecodeError, read_wav
from ..pipeline import PipelineError, failure_report, run_pipeline
from .schemas import HealthResponse, JobCreateRequest, JobDetail, JobSummary

logger = get_logger("api")


def _to_summary(rec: JobRecord) -> JobSummary:
    return JobSummary(
        job_id=rec.job_id, request_id=rec.request_id, status=rec.status.value,
        created_at=rec.created_at, updated_at=rec.updated_at,
        error_class=rec.error_class, error_message=rec.error_message,
        pipeline_version=rec.pipeline_version,
    )


def _to_detail(rec: JobRecord) -> JobDetail:
    return JobDetail(**_to_summary(rec).model_dump(),
                     params=rec.params, result=rec.result)


def create_app(cfg: AppConfig | None = None) -> FastAPI:
    cfg = cfg or load_config()
    store = JobStore(cfg.db_path)
    app = FastAPI(title="driftcorr", version=__version__)
    app.state.cfg = cfg
    app.state.store = store

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        log_step(logger, request_id=request_id, job_id="-", step="http",
                 message=f"{request.method} {request.url.path} -> {response.status_code}")
        return response

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__,
                              pipeline_version=PIPELINE_VERSION)

    @app.post("/jobs", response_model=JobDetail, status_code=201)
    def create_job(req: JobCreateRequest, request: Request) -> JobDetail:
        request_id: str = request.state.request_id
        params = req.model_dump()

        for label, p in (("reference_path", req.reference_path),
                         ("target_path", req.target_path),
                         ("reference_metadata_path", req.reference_metadata_path)):
            if not Path(p).exists():
                return JSONResponse(  # type: ignore[return-value]
                    status_code=400,
                    content={"error_class": "invalid_input",
                             "message": f"{label} does not exist: {p}",
                             "request_id": request_id},
                )

        rec = store.create(request_id=request_id, params=params)
        store.mark_running(rec.job_id)
        try:
            report = run_pipeline(
                job_id=rec.job_id, request_id=request_id,
                reference=read_wav(req.reference_path),
                target=read_wav(req.target_path),
                reference_meta=load_metadata(req.reference_metadata_path),
                target_meta=(load_metadata(req.target_metadata_path)
                             if req.target_metadata_path else None),
                cfg=cfg,
            )
        except (PipelineError, MediaDecodeError, MetadataError) as exc:
            error_class = getattr(exc, "error_class", type(exc).__name__)
            log_step(logger, request_id=request_id, job_id=rec.job_id,
                     step="pipeline", message="pipeline failed",
                     error_class=error_class, error=str(exc))
            report = failure_report(rec.job_id, request_id, exc)
        store.save_result(rec.job_id, report)
        return _to_detail(store.get(rec.job_id))  # type: ignore[arg-type]

    @app.get("/jobs", response_model=list[JobSummary])
    def list_jobs(limit: int = 50) -> list[JobSummary]:
        return [_to_summary(r) for r in store.list(limit=limit)]

    @app.get("/jobs/{job_id}", response_model=JobDetail)
    def get_job(job_id: str, request: Request):
        rec = store.get(job_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error_class": "job_not_found",
                         "message": f"no job with id {job_id}",
                         "request_id": request.state.request_id},
            )
        return _to_detail(rec)

    return app


app = create_app()
