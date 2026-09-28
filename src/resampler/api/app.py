"""FastAPI application: validation, job lifecycle, chunked resampling."""

from __future__ import annotations

import base64
import os
import time
import uuid
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, Response as RawResponse

from ..config import Settings
from ..errors import ResamplerError
from ..jobs import JobManager, JobParams, JobStore
from ..media.pcm import encode_pcm
from ..media.wav import build_wav
from ..observability import RunLogger
from ..signal import build_plan
from .schemas import (ChunkJsonRequest, ChunkResponse, CreateJobRequest,
                      FlushResponse, GroupDelay, JobResponse, StatusResponse,
                      ValidateRequest, ValidateResponse)

settings = Settings.load()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Tests/integration may pre-inject app.state.settings; don't clobber it.
    st = getattr(app.state, "settings", None) or settings
    app.state.settings = st
    os.makedirs(os.path.dirname(os.path.abspath(st.db_path)) or ".",
                exist_ok=True)
    os.makedirs(st.log_dir, exist_ok=True)
    pre_logger = getattr(app.state, "logger", None)
    server_run = pre_logger or RunLogger(st.log_dir,
                                         run_id="server-" + time.strftime("%Y%m%dT%H%M%S"))
    app.state.store = getattr(app.state, "store", None) or JobStore(st.db_path)
    app.state.logger = server_run
    app.state.manager = getattr(app.state, "manager", None) or \
        JobManager(st, app.state.store, server_run)
    server_run.event("server_started", {"settings": st.to_dict()})
    yield
    app.state.store.close()
    server_run.summary("shutdown")


app = FastAPI(
    title="Rational polyphase resampler",
    version="1.0.0",
    description="Mono PCM rational-ratio streaming resampling service.",
    lifespan=lifespan,
)


def _manager(req: Request) -> JobManager:
    return req.app.state.manager


def _run_id(x_run_id: str | None) -> str:
    rid = x_run_id or time.strftime("run-%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8]
    return rid[:64]


def _request_logger(req: Request, run_id: str) -> RunLogger:
    return RunLogger(req.app.state.settings.log_dir, run_id=run_id)


@app.exception_handler(ResamplerError)
async def resampler_error_handler(request: Request, exc: ResamplerError):
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


from fastapi.exceptions import RequestValidationError  # noqa: E402


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Schema-level rejections are input errors (400) in the service taxonomy."""
    return JSONResponse(status_code=400, content={
        "error": {
            "category": "input_error",
            "code": "invalid_input",
            "message": "request schema validation failed",
            "detail": {"errors": exc.errors()},
        }})


@app.get("/health")
async def health():
    return {"status": "ok", "service": "rational-resampler", "version": "1.0.0"}


@app.post("/resample/validate", response_model=ValidateResponse)
async def validate(body: ValidateRequest, request: Request,
                   x_run_id: str | None = Header(default=None)):
    rid = _run_id(x_run_id)
    log = _request_logger(request, rid)
    log.event("validate", {"in": body.model_dump()})
    plan = build_plan(body.input_rate, body.output_rate,
                      atten_db=body.attenuation_db,
                      passband_edge=body.passband_edge,
                      settings=request.app.state.settings)
    d = plan.describe()
    return ValidateResponse(
        up=d["up"], down=d["down"], taps_per_phase=d["taps_per_phase"],
        num_taps=d["num_taps"], attenuation_db=d["attenuation_db"],
        passband_edge_fraction=d["passband_edge_fraction"],
        passband_edge_hz=d["passband_edge_hz"],
        stopband_edge_hz=d["stopband_edge_hz"], cutoff_hz=d["cutoff_hz"],
        group_delay=GroupDelay(**d["group_delay"]),
        padding={
            "head_zeros_input_samples": plan.taps_per_phase - 1,
            "tail_zeros_input_samples": plan.taps_per_phase - 1,
            "strategy": "fixed zero padding; flush() releases the tail",
        },
        note=("output n aligns with input time "
              "t(n)=(n*down-(K-1)/2)/(up*f_in); trim the leading delay "
              "for zero-phase alignment with the input timeline."),
    )


def _params(body: CreateJobRequest) -> JobParams:
    return JobParams(
        input_rate=body.input_rate, output_rate=body.output_rate,
        input_format=body.input_format, input_container=body.input_container,
        output_format=body.output_format, output_container=body.output_container,
        clip_policy=body.clip_policy, atten_db=body.attenuation_db,
        passband_edge=body.passband_edge)


@app.post("/jobs", response_model=JobResponse, status_code=201)
async def create_job(body: CreateJobRequest, request: Request,
                     x_run_id: str | None = Header(default=None)):
    rid = _run_id(x_run_id)
    log = _request_logger(request, rid)
    mgr = _manager(request)
    row = mgr.create_job(_params(body), job_id=body.job_id, logger=log)
    plan = build_plan(row.input_rate, row.output_rate, atten_db=row.atten_db,
                      passband_edge=row.passband_edge,
                      settings=request.app.state.settings)
    return JobResponse(
        job_id=row.job_id, state=row.state, plan=plan.describe(),
        input_format=row.input_format, input_container=row.input_container,
        output_format=row.output_format, output_container=row.output_container,
        clip_policy=row.clip_policy, run_id=rid)


@app.post("/jobs/{job_id}/chunks", response_model=ChunkResponse)
async def add_chunk(job_id: str, request: Request,
                    x_run_id: str | None = Header(default=None),
                    x_container: str | None = Header(default=None)):
    rid = _run_id(x_run_id)
    log = _request_logger(request, rid)
    raw = await request.body()
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip()
    if ctype == "application/json":
        parsed = ChunkJsonRequest.model_validate_json(raw)
        data = base64.b64decode(parsed.data_b64, validate=True)
    else:
        data = raw
    mgr = _manager(request)
    out = mgr.add_chunk(job_id, data, logger=log)
    return ChunkResponse(job_id=job_id, run_id=rid, **out)


@app.post("/jobs/{job_id}/flush", response_model=FlushResponse)
async def flush_job(job_id: str, request: Request,
                    x_run_id: str | None = Header(default=None)):
    rid = _run_id(x_run_id)
    log = _request_logger(request, rid)
    out = _manager(request).flush(job_id, logger=log)
    return FlushResponse(job_id=job_id, run_id=rid, **out)


@app.get("/jobs/{job_id}")
async def job_status(job_id: str, request: Request):
    row = _manager(request).store.get_job(job_id)
    if row is None:
        from ..errors import InputValidationError
        raise InputValidationError("unknown job_id", {"job_id": job_id})
    plan = build_plan(row.input_rate, row.output_rate, atten_db=row.atten_db,
                      passband_edge=row.passband_edge,
                      settings=request.app.state.settings)
    err = None
    if row.error_category:
        err = {"category": row.error_category, "code": row.error_code,
               "message": row.error_message}
    return StatusResponse(
        job_id=row.job_id, state=row.state, input_samples=row.input_samples,
        output_samples=row.output_samples, clipped_samples=row.clipped_samples,
        chunks_received=row.chunks_received, error=err, plan=plan.describe())


@app.get("/jobs/{job_id}/result")
async def job_result(job_id: str, request: Request):
    """Full resampled result. Default: raw PCM bytes; ?format=json for base64."""
    row, f64, clipped = _manager(request).get_result(job_id)
    fmt = request.query_params.get("format", "raw")
    encoded = encode_pcm(f64, row.output_format, clip_policy=row.clip_policy)
    media_types = {"u8": "audio/pcm;rate=8000", "s16le": "audio/L16",
                   "s24le": "application/octet-stream",
                   "s32le": "application/octet-stream",
                   "f32le": "application/octet-stream",
                   "f64le": "application/octet-stream"}
    if row.output_container == "wav":
        payload = build_wav(f64, row.output_rate, row.output_format)
        media_type = "audio/wav"
    else:
        payload = encoded.data
        media_type = media_types[row.output_format]
    headers = {"X-Output-Samples": str(f64.size),
               "X-Clipped-Samples": str(encoded.clipped),
               "X-Group-Delay-Input": str(row.delay_input),
               "X-Group-Delay-Output": str(row.delay_output)}
    if fmt == "json":
        body = {"job_id": job_id, "output_format": row.output_format,
                "container": row.output_container, "samples": int(f64.size),
                "clipped_samples": int(encoded.clipped),
                "data_b64": base64.b64encode(payload).decode("ascii")}
        return JSONResponse(body, headers=headers)
    return RawResponse(content=payload, media_type=media_type, headers=headers)
