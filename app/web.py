"""FastAPI 路由、统一错误信封、run_id 中间件。"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Settings
from .errors import (ComputationFailedError, InputInvalidError,
                     SegmentError, error_envelope)
from .schemas import (CreateJob, MediaSpec, SegmentConfigIn)
from .service import SegmentService
from .store import JobStore


def new_run_id() -> str:
    return f"run-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:12]}"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    db_path = str(Path(settings.data_dir) / "jobs.db")
    store = JobStore(db_path, event_ring=settings.event_ring)
    service = SegmentService(store, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.store = store
        app.state.service = service
        yield
        store.close()

    app = FastAPI(title="Offline PCM Dual-Threshold Silence Segmenter",
                  version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.service = service

    # --------------------------------------------------------- run_id 中间件

    @app.middleware("http")
    async def attach_run_id(request: Request, call_next):
        request.state.run_id = request.headers.get("X-Run-ID") or new_run_id()
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Run-ID"] = request.state.run_id
        response.headers["X-Server-Time-ms"] = \
            f"{(time.perf_counter() - start) * 1000:.2f}"
        return response

    # ------------------------------------------------------------- 错误处理

    @app.exception_handler(SegmentError)
    async def segment_error_handler(request: Request, exc: SegmentError):
        return JSONResponse(
            status_code=exc.http_status,
            content=error_envelope(exc.code, exc.message,
                                   request.state.run_id, exc.details))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request,
                                 exc: RequestValidationError):
        # pydantic 的 errors() 在 ctx 里可能带原始异常对象，先净化为字符串
        safe_errors = []
        for e in exc.errors():
            e = dict(e)
            ctx = e.get("ctx")
            if ctx:
                e["ctx"] = {k: str(v) for k, v in ctx.items()}
            safe_errors.append(e)
        return JSONResponse(
            status_code=422,
            content=error_envelope(
                "INPUT_INVALID", "request validation failed",
                request.state.run_id, {"errors": safe_errors}))

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content=error_envelope(
                "COMPUTATION_FAILED", f"{type(exc).__name__}: {exc}",
                request.state.run_id, {}))

    # ----------------------------------------------------------------- 路由

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/jobs", status_code=201)
    async def create_job(body: CreateJob, request: Request):
        rid = request.state.run_id
        jid = service.create_job(body.config, body.media.model_dump(), rid)
        return {"job_id": jid, "run_id": rid,
                **service.describe(jid)}

    @app.post("/jobs/{job_id}/chunks")
    async def add_chunk(job_id: str, request: Request,
                        finalize: bool = Query(default=False)):
        rid = request.state.run_id
        payload = await request.body()
        result = service.ingest_chunk(job_id, payload, rid,
                                      finalize=finalize)
        result["run_id"] = rid
        return result

    @app.post("/jobs/{job_id}/finalize")
    async def finalize_job(job_id: str, request: Request):
        rid = request.state.run_id
        result = service.finalize(job_id, rid)
        result["run_id"] = rid
        return result

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, request: Request):
        result = service.describe(job_id)
        result["run_id"] = request.state.run_id
        return result

    @app.get("/jobs/{job_id}/events")
    async def get_events(job_id: str, request: Request,
                         limit: int = Query(default=500, ge=1, le=2000)):
        return {"job_id": job_id, "run_id": request.state.run_id,
                "events": service.events(job_id, limit)}

    @app.post("/jobs/{job_id}/verify")
    async def verify_job(job_id: str, request: Request):
        rid = request.state.run_id
        result = service.verify(job_id, rid)
        result["run_id"] = rid
        return result

    # 一次性便捷接口：上传完整 WAV，query 传配置
    @app.post("/segment")
    async def segment_one_shot(
        request: Request,
        enter_threshold: float | None = Query(default=None),
        exit_threshold: float | None = Query(default=None),
        enter_db: float | None = Query(default=None),
        exit_db: float | None = Query(default=None),
        min_silence_ms: float = Query(default=200.0),
        min_speech_ms: float = Query(default=50.0),
        pad_before_ms: float = Query(default=10.0),
        pad_after_ms: float = Query(default=20.0),
        merge_gap_ms: float = Query(default=0.0),
        edge_keep: bool = Query(default=True),
    ):
        rid = request.state.run_id
        cfg = SegmentConfigIn(
            enter_threshold=enter_threshold,
            exit_threshold=exit_threshold,
            enter_threshold_db=enter_db,
            exit_threshold_db=exit_db,
            min_silence_ms=min_silence_ms,
            min_speech_ms=min_speech_ms,
            pad_before_ms=pad_before_ms,
            pad_after_ms=pad_after_ms,
            merge_gap_ms=merge_gap_ms,
            edge_keep=edge_keep)
        payload = await request.body()
        if not payload:
            raise InputInvalidError("empty WAV body")
        jid = service.create_job(cfg, {"container": "wav"}, rid)
        result = service.ingest_chunk(jid, payload, rid)
        result["run_id"] = rid
        return result

    return app


app = create_app()
