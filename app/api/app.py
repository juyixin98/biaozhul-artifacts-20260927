"""FastAPI application factory and cross-cutting middleware."""
from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..config import Settings
from ..core.analyzer import analyze
from ..jobs.manager import JobManager
from .routes import router

logger = logging.getLogger("mtsa")


def create_app(settings: Settings | None = None,
               start_worker: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()

    app = FastAPI(
        title="Local MPEG-TS Analysis Backend",
        version="1.0.0",
        description="Synchronous validation and asynchronous analysis of "
                    "local MPEG-TS streams (PAT/PMT/PES, continuity checks).")
    app.state.settings = settings
    app.state.jobs = JobManager(
        settings,
        processor=lambda data, rid: analyze(data, rid, settings).to_dict(),
        start_worker=start_worker)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        # Default request id for routes that do not set it themselves.
        request.state.request_id = (
            request.headers.get("x-request-id") or uuid.uuid4().hex)
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled error request_id=%s path=%s",
                             request.state.request_id,
                             request.url.path)
            return JSONResponse(
                status_code=500,
                content={"error": "internal_error",
                         "request_id": request.state.request_id})
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(404)
    async def not_found(request: Request, exc):  # noqa: ANN001
        # Preserve an explicit HTTPException body (e.g. job_not_found); only
        # fall back to a generic body for framework-level unknown routes.
        detail = getattr(exc, "detail", None)
        if isinstance(detail, dict) and "error" in detail:
            content = detail
        else:
            content = {"error": "not_found", "path": request.url.path}
        content.setdefault(
            "request_id", getattr(request.state, "request_id", None))
        return JSONResponse(status_code=404, content=content)

    app.include_router(router)
    return app


app = create_app()
