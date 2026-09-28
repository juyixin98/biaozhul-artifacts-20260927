"""FastAPI application wiring and HTTP endpoints."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import diagnostics as diag
from .config import PROJECT_ROOT, Settings
from .diagnostics import DiagnosticRecorder, new_request_id
from .schemas import (
    ErrorResponse,
    PublishRequest,
    PublishResponse,
    SegmentRequest,
    VersionOut,
    to_response,
)
from .service import SegService, ServiceError
from .store import VersionStore


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = FastAPI(
        title="Optimal Lexicon Segmentation Backend",
        version="1.0.0",
        description=(
            "Frequency-cost word segmentation over a DAG. Returns the optimal "
            "segmentation, the second-best cost gap, raw<->normalized offsets "
            "and version-pinned results."
        ),
    )
    store = VersionStore(settings.db_path)
    recorder = DiagnosticRecorder()
    service = SegService(store, settings, recorder)

    if settings.seed_on_start:
        seed = PROJECT_ROOT / "data" / "seed_lexicon.json"
        if seed.exists():
            store.seed_from_json(seed)

    app.state.settings = settings
    app.state.store = store
    app.state.recorder = recorder
    app.state.service = service

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        # Honor a caller-provided id for trace correlation, else mint one.
        rid = request.headers.get("X-Request-ID") or new_request_id()
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        rid = getattr(request.state, "request_id", new_request_id())
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                request_id=rid, error=exc.reason, message=exc.message
            ).model_dump(),
            headers={"X-Request-ID": rid},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # Malformed bodies are a rejection with an explicit category.
        rid = getattr(request.state, "request_id", new_request_id())
        fields = sorted({
            ".".join(str(x) for x in e["loc"] if x != "body")
            for e in exc.errors()
        })
        message = "invalid request payload" + (
            ": " + ",".join(f for f in fields if f) if fields else ""
        )
        recorder.record(
            diag.make_error_diagnostic(
                request_id=rid, outcome=diag.REJECTED,
                reason=diag.REASON_INVALID_PAYLOAD, detail=message,
            )
        )
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                request_id=rid, error=diag.REASON_INVALID_PAYLOAD, message=message
            ).model_dump(),
            headers={"X-Request-ID": rid},
        )

    @app.get("/health")
    def health(request: Request):
        latest = store.latest_version_id()
        return {
            "status": "ok" if latest is not None else "no_lexicon",
            "latest_version": latest,
            "request_id": request.state.request_id,
        }

    @app.post("/segment")
    def segment_endpoint(body: SegmentRequest, request: Request):
        rid = request.state.request_id
        result, version_id, pinned = service.segment_text(
            body.text, body.version_id, rid
        )
        return to_response(
            request_id=rid, result=result, version_id=version_id,
            pinned=pinned, raw_len=len(body.text),
        )

    @app.post("/versions", response_model=PublishResponse, status_code=201)
    def publish_endpoint(body: PublishRequest, request: Request):
        rid = request.state.request_id
        built = service.publish(
            [w.model_dump() for w in body.words], body.note, rid
        )
        return PublishResponse(
            request_id=rid, version_id=built.version_id,
            word_count=built.word_count, total_freq=built.total_freq,
            checksum=built.checksum,
        )

    @app.get("/versions", response_model=list[VersionOut])
    def list_versions_endpoint():
        return store.list_versions()

    @app.get("/diagnostics")
    def diagnostics_endpoint(request: Request, limit: int = 50, request_id: str | None = None):
        if request_id:
            item = recorder.get(request_id)
            return {"request_id": request.state.request_id, "record": item}
        return {"request_id": request.state.request_id, "records": recorder.recent(limit)}

    return app


app = create_app()
