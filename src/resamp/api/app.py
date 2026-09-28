"""FastAPI application factory: service lifecycle, request ids, error mapping."""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..config import Settings, load_settings
from ..errors import ResampError
from ..service import ResamplingService
from .routes import router

log = logging.getLogger("resamp")


def _sanitize(obj):
    """Make error details JSON-safe (NaN/Inf are not JSON-legal)."""
    import math
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else f"non-finite:{obj}"
    if isinstance(obj, dict):
        return {str(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _error_body(exc: ResampError, request_id: str) -> dict:
    body = dict(exc.to_dict())
    body["details"] = _sanitize(exc.details)
    body["request_id"] = request_id
    return body


def create_app(settings: Settings | None = None,
               service: ResamplingService | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    svc = service or ResamplingService(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        svc.close()

    app = FastAPI(
        title="resamp — rational polyphase FIR resampling",
        version="1.0.0",
        description="Mono PCM rational-ratio resampling with chunked input "
                    "and explicit flush.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = svc

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error request_id=%s path=%s",
                          request_id, request.url.path)
            raise
        response.headers["x-request-id"] = request_id
        response.headers["x-duration-ms"] = f"{(time.perf_counter()-started)*1000:.2f}"
        return response

    @app.exception_handler(ResampError)
    async def resamp_error_handler(request: Request, exc: ResampError):
        rid = getattr(request.state, "request_id", "-")
        log.warning("domain error request_id=%s code=%s msg=%s details=%s",
                    rid, exc.error_code, exc.message, exc.details)
        return JSONResponse(status_code=exc.http_status,
                            content=_error_body(exc, rid))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        rid = getattr(request.state, "request_id", "-")
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request",
                     "category": "invalid_input",
                     "message": "request schema validation failed",
                     "details": {"errors": exc.errors()},
                     "request_id": rid},
        )

    # Register routes.
    app.include_router(router)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": app.version}

    return app


app = create_app()
