"""FastAPI interface: synchronous validation endpoints and async job API.

Endpoints
---------
GET  /health                       liveness + algorithm/worker identity
GET  /version                      explicit support matrix (incl. true peak)
POST /measurements/pcm             raw little-endian PCM, explicit descriptor
POST /measurements/wav             uncompressed WAV upload (sync result)
POST /jobs                         uncompressed WAV upload (async job)
GET  /jobs/{job_id}                job state + result/failure category
GET  /jobs                         recent jobs (diagnostics)
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from .config import get_settings, Settings
from .jobs import JOB_PROCESSING, JobStore
from .log import configure_logging
from .media import MediaError, decode_raw_pcm, decode_wav
from .service import measure
from .validation import ValidationError, validate_raw_pcm_params

SUPPORTED = {
    "integrated_loudness": True,
    "loudness_range_lra": True,
    "momentary_loudness": True,
    "short_term_loudness": True,
    "true_peak_tpbs1770": False,
    "compressed_media_mp3_aac_opus": False,
    "channel_layouts": ["mono", "stereo", "5.0", "5.1 (LFE excluded)"],
    "pcm_formats": ["s16", "s24", "s32", "f32", "u8 (WAV only)"],
}


def _settings_dep() -> Settings:
    return get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = _settings_dep()
    app.state.settings = settings
    app.state.logger = configure_logging(settings)
    app.state.jobs = JobStore(settings.db_path, settings.worker_id)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="EBU R128 integrated loudness & LRA backend",
        version="1.0.0",
        lifespan=lifespan,
    )

    def _request_id(request: Request) -> str:
        return request.headers.get("x-request-id") or uuid.uuid4().hex

    def _fail(status_code: int, code: str, message: str, request_id: str,
              log: logging.Logger, step: str, job_id: str | None = None,
              extra: dict | None = None) -> JSONResponse:
        log.warning(message, extra={"step": step, "request_id": request_id,
                                    "job_id": job_id or "-",
                                    "context": {"code": code, **(extra or {})}})
        body: dict = {"request_id": request_id,
                      "error": {"code": code, "message": message, "step": step}}
        if job_id is not None:
            body["job_id"] = job_id
        return JSONResponse(status_code=status_code, content=body)

    def _success(request_id: str, result: dict, log: logging.Logger,
                 step: str, job_id: str | None = None) -> JSONResponse:
        log.info("measurement complete",
                 extra={"step": step, "request_id": request_id,
                        "job_id": job_id or "-",
                        "context": {"status": result["status"],
                                    "integrated_lufs": result["integrated_loudness"]["integrated_lufs"],
                                    "lra_lu": result["loudness_range"]["lra_lu"],
                                    "worker": result["provenance"]["worker_id"]}})
        return JSONResponse(content={"request_id": request_id, "result": result})

    # -- health / version -------------------------------------------------

    @app.get("/health")
    async def health(request: Request):
        settings = request.app.state.settings
        return {"status": "ok",
                "request_id": _request_id(request),
                "service": settings.service_name,
                "algorithm_id": settings.algorithm_id,
                "worker_id": settings.worker_id}

    @app.get("/version")
    async def version():
        settings = _settings_dep()
        return {"algorithm_id": settings.algorithm_id,
                "spec_refs": list(settings.algorithm_spec_refs),
                "supported": SUPPORTED}

    # -- raw PCM ----------------------------------------------------------

    @app.post("/measurements/pcm")
    async def measure_pcm(
        request: Request,
        sample_rate: int = Form(...),
        channels: int = Form(...),
        sample_format: str = Form(...),
        layout: str | None = Form(None),
        include_blocks: str | None = Form(None),
        label: str | None = Form(None),
        payload: UploadFile = Form(...),
    ):
        settings = request.app.state.settings
        log = request.app.state.logger
        request_id = _request_id(request)
        step = "validate_pcm"
        data = await payload.read()
        if len(data) > settings.max_payload_bytes:
            return _fail(413, "PAYLOAD_TOO_LARGE",
                         f"payload {len(data)} bytes exceeds limit", request_id, log, step)
        try:
            descriptor = validate_raw_pcm_params(
                sample_rate=sample_rate, channels=channels,
                sample_format=sample_format, layout=layout,
                include_blocks=include_blocks or "false", label=label,
                payload_size=len(data), settings=settings)
            step = "parse_pcm"
            decoded = decode_raw_pcm(
                data, sample_rate=descriptor.sample_rate,
                channels=descriptor.channels, sample_format=descriptor.sample_format)
        except (ValidationError, MediaError) as exc:
            return _fail(422, exc.code, exc.message, request_id, log, step)

        step = "measure"
        result = measure(decoded, request_id=request_id, settings=settings,
                         include_blocks=descriptor.include_blocks, label=label)
        return _success(request_id, result, log, step)

    # -- WAV (sync + async job) ------------------------------------------

    async def _read_wav(request: Request, payload: UploadFile):
        settings = request.app.state.settings
        data = await payload.read()
        if len(data) > settings.max_payload_bytes:
            raise MediaError("PAYLOAD_TOO_LARGE",
                             f"payload {len(data)} bytes exceeds limit")
        return data

    @app.post("/measurements/wav")
    async def measure_wav(
        request: Request,
        payload: UploadFile = Form(...),
        include_blocks: str | None = Form(None),
        label: str | None = Form(None),
        layout: str | None = Form(None),
    ):
        log = request.app.state.logger
        request_id = _request_id(request)
        try:
            data = await _read_wav(request, payload)
            decoded = decode_wav(data, layout_override=layout)
        except MediaError as exc:
            return _fail(422, exc.code, exc.message, request_id, log, "parse_wav")
        include = include_blocks and include_blocks.lower() in ("1", "true", "yes", "on")
        result = measure(decoded, request_id=request_id,
                         settings=request.app.state.settings,
                         include_blocks=include, label=label)
        return _success(request_id, result, log, "measure")

    @app.post("/jobs")
    async def create_job(
        request: Request,
        payload: UploadFile = Form(...),
        include_blocks: str | None = Form(None),
        label: str | None = Form(None),
        layout: str | None = Form(None),
    ):
        settings: Settings = request.app.state.settings
        log: logging.Logger = request.app.state.logger
        store: JobStore = request.app.state.jobs
        request_id = _request_id(request)
        include = include_blocks and include_blocks.lower() in ("1", "true", "yes", "on")
        job_id = store.create(request_id=request_id, input_kind="wav",
                              include_blocks=include, label=label)
        log.info("job created", extra={"step": "job_create", "request_id": request_id,
                                       "job_id": job_id})

        # Local in-process synchronous execution (state transitions still go
        # through the persisted job store); no external broker is involved.
        try:
            data = await _read_wav(request, payload)
        except MediaError as exc:
            store.mark_failed(job_id, exc.code, exc.message)
            return _fail(422, exc.code, exc.message, request_id, log,
                         "parse_wav", job_id=job_id)

        try:
            store.mark_processing(job_id)
            decoded = decode_wav(data, layout_override=layout)
        except MediaError as exc:
            store.mark_failed(job_id, exc.code, exc.message)
            return _fail(422, exc.code, exc.message, request_id, log,
                         "parse_wav", job_id=job_id)

        result = measure(decoded, request_id=request_id, settings=settings,
                         include_blocks=include, label=label)
        store.mark_succeeded(job_id, result)
        log.info("job succeeded", extra={"step": "job_finish",
                                         "request_id": request_id, "job_id": job_id,
                                         "context": {"status": result["status"]}})
        return JSONResponse(status_code=202,
                            content={"request_id": request_id,
                                     "job_id": job_id,
                                     "status": JOB_PROCESSING,
                                     "location": f"/jobs/{job_id}"})

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, request: Request):
        store: JobStore = request.app.state.jobs
        job = store.get(job_id)
        if job is None:
            return _fail(404, "JOB_NOT_FOUND", f"no job with id {job_id}",
                         _request_id(request), request.app.state.logger,
                         "job_lookup", job_id=job_id)
        return {"request_id": job["request_id"], "job": job}

    @app.get("/jobs")
    async def list_jobs(request: Request, limit: int = 50):
        store: JobStore = request.app.state.jobs
        return {"jobs": store.list_recent(min(max(limit, 1), 200))}

    return app


app = create_app()
