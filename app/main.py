"""FastAPI application: EBU R128 loudness / LRA backend.

Endpoints
---------
GET  /health                          version + kernel identity
POST /analyze/wav                     one-shot measurement of an uploaded WAV
POST /jobs                           create a chunked raw-PCM job
POST /jobs/{id}/chunks                append raw PCM bytes
POST /jobs/{id}/finalize              finish and read the measurement
GET  /jobs/{id}                       fetch stored result/error
GET  /jobs                            recent jobs (observability)

Every response carries ``request_id`` (client-supplied ``X-Request-ID`` or a
generated one), ``kernel_version`` and ``processed_at``; failures carry a
stable ``error.code`` from app/errors.py.
"""
from __future__ import annotations

import json
import os
import time
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .config import settings
from .errors import R128Error
from .jobs import JobManager
from .jobstore import JobStore
from .logging_setup import logger, set_request_context
from .loudness import analyze_array
from .media import parse_wav
from .r128_constants import KERNEL_VERSION, SPEC_ID
from .schemas import JobCreateRequest
from .serialization import result_to_dict

PROCESSED_AT = f"pid={os.getpid()}@r128-local"

app = FastAPI(
    title="EBU R128 loudness backend",
    version=KERNEL_VERSION,
    description="Integrated gated loudness and loudness range (LRA) for "
                "48 kHz PCM, measured per ITU-R BS.1770-4 / EBU R128 / "
                "Tech 3342. True-peak is deliberately not measured.",
)

_store: JobStore | None = None
_jobs: JobManager | None = None


def init_resources(db_path: str, max_bytes: int | None = None) -> None:
    """(Re)create the job store and manager. Used at startup and by tests."""
    global _store, _jobs
    if _store is not None:
        _store.close()
    _store = JobStore(db_path)
    _jobs = JobManager(_store, max_bytes or settings.max_job_bytes)


init_resources(settings.db_path)


def _request_id(request: Request) -> str:
    # Middleware stamps request.state; the helper also handles direct calls.
    return (getattr(request.state, "request_id", None)
            or request.headers.get("X-Request-ID")
            or f"req-{uuid.uuid4().hex[:12]}")


def _envelope(request_id: str, body: dict, status_code: int = 200) -> JSONResponse:
    body = {
        "request_id": request_id,
        "kernel_version": KERNEL_VERSION,
        "specification": SPEC_ID,
        "processed_at": PROCESSED_AT,
        **body,
    }
    return JSONResponse(body, status_code=status_code)


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    rid = _request_id(request)
    request.state.request_id = rid
    set_request_context(rid)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception({"event": "unhandled_exception",
                          "path": request.url.path})
        raise
    dt_ms = (time.perf_counter() - start) * 1000.0
    response.headers["X-Request-ID"] = rid
    logger.info({
        "event": "request",
        "path": request.url.path,
        "method": request.method,
        "status_code": response.status_code,
        "duration_ms": round(dt_ms, 2),
    })
    return response


@app.exception_handler(R128Error)
async def r128_error_handler(request: Request, exc: R128Error) -> JSONResponse:
    rid = _request_id(request)
    logger.warning({
        "event": "request_failed",
        "path": request.url.path,
        "error_code": exc.code,
        "error_message": exc.message,
        "details": exc.details,
    })
    return _envelope(rid, {
        "error": {
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    }, status_code=exc.http_status)


@app.get("/health")
async def health(request: Request) -> Response:
    rid = _request_id(request)
    return _envelope(rid, {
        "service": "r128-loudness-backend",
        "status": "healthy",
        "db_path": settings.db_path,
        "max_job_bytes": settings.max_job_bytes,
    })


@app.post("/analyze/wav")
async def analyze_wav(request: Request) -> Response:
    rid = _request_id(request)
    raw = await request.body()
    if len(raw) > settings.max_job_bytes:
        from .errors import JobError
        raise JobError(
            f"WAV payload {len(raw)} bytes exceeds limit "
            f"{settings.max_job_bytes}",
            details={"limit": settings.max_job_bytes, "bytes": len(raw)},
        )
    parsed = parse_wav(raw)
    logger.info({
        "event": "wav_parsed",
        "channels": parsed.channels,
        "sample_format": parsed.sample_format,
        "frames": parsed.samples.shape[0],
        "bytes": len(raw),
    })
    result = analyze_array(parsed.samples)
    payload = result_to_dict(result)
    payload["input"] = {
        "container": "WAVE",
        "sample_format": parsed.sample_format,
        "extensible": parsed.is_extensible,
        "bytes": len(raw),
    }
    if result.status != "OK" or result.lra.status != "OK":
        logger.info({
            "event": "partial_result",
            "integrated_status": result.status,
            "lra_status": result.lra.status,
            "warnings": result.warnings,
        })
    return _envelope(rid, {"result": payload})


@app.post("/jobs", status_code=201)
async def create_job(model: JobCreateRequest, request: Request) -> Response:
    rid = _request_id(request)
    if model.channels not in settings.allowed_channel_counts:
        from .errors import UnsupportedLayoutError
        raise UnsupportedLayoutError(
            f"{model.channels} channels not allowed",
            details={"allowed": list(settings.allowed_channel_counts)},
        )
    roles = model.roles
    if roles is None:
        roles = settings.default_layouts[model.channels]
    job_id = _jobs.create(
        channels=model.channels, sample_format=model.sample_format,
        roles=roles, request_id=rid,
    )
    logger.info({"event": "job_created", "job_id": job_id,
                 "channels": model.channels, "sample_format": model.sample_format})
    return _envelope(rid, {"job_id": job_id, "state": "OPEN",
                           "roles": roles}, status_code=201)


@app.post("/jobs/{job_id}/chunks")
async def append_chunk(job_id: str, request: Request) -> Response:
    rid = _request_id(request)
    set_request_context(rid, job_id)
    raw = await request.body()
    _jobs.append(job_id, raw)
    return _envelope(rid, {"job_id": job_id, "bytes_this_chunk": len(raw),
                           "state": "OPEN"})


@app.post("/jobs/{job_id}/finalize")
async def finalize_job(job_id: str, request: Request) -> Response:
    rid = _request_id(request)
    set_request_context(rid, job_id)
    payload = _jobs.finalize(job_id)
    logger.info({
        "event": "job_finalized",
        "job_id": job_id,
        "status": payload["status"],
        "lra_status": payload["loudness_range"]["status"],
        "integrated_lufs": payload["integrated_loudness_lufs"],
        "lra_lu": payload["loudness_range"]["lra_lu"],
    })
    return _envelope(rid, {"job_id": job_id, "result": payload})


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> Response:
    rid = _request_id(request)
    row = _store.get(job_id)
    if row is None:
        from .errors import JobError
        raise JobError("unknown job id", details={"job_id": job_id})
    body = {
        "job_id": job_id,
        "state": row["state"],
        "bytes_received": row["bytes_received"],
        "processing_ms": row["processing_ms"],
    }
    if row["result_json"]:
        body["result"] = json.loads(row["result_json"])
    if row["error_code"]:
        body["error"] = {"code": row["error_code"],
                         "message": row["error_message"]}
    return _envelope(rid, body)


@app.get("/jobs")
async def list_jobs(request: Request) -> Response:
    rid = _request_id(request)
    rows = _store.list_recent()

    def _stored_lufs(row) -> float | None:
        if not row["result_json"]:
            return None
        return json.loads(row["result_json"]).get("integrated_loudness_lufs")

    items = [{
        "job_id": r["id"], "state": r["state"],
        "request_id": r["request_id"],
        "bytes_received": r["bytes_received"],
        "error_code": r["error_code"],
        "integrated_lufs": _stored_lufs(r),
    } for r in rows]
    return _envelope(rid, {"jobs": items, "count": len(items)})
