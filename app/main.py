"""FastAPI application factory + diagnostic middleware.

Middleware responsibilities (in order):

1. assign/echo a request id;
2. create the per-request :class:`~app.diagnostics.Recorder`;
3. persist one ``request`` event with key inbound state (sizes only — bodies
   are never logged);
4. on a :class:`DomainError`, persist a ``decision`` event explaining why the
   request was rejected or is inconclusive and return the typed error body;
5. on any unexpected exception, persist an ``inconclusive`` decision (no
   internals leaked to the client) and return 500.

Note on Starlette 0.46: handlers registered via ``add_exception_handler`` are
invoked as ``handler(conn, exc)`` where ``conn`` is the ASGI ``Connection``
(not a ``Request``); ``conn.request`` yields the concrete request.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api.deps import Container, resolve_request_id
from .api.routes_diagnostics import router as diag_router
from .api.routes_scans import router as scans_router
from .api.routes_versions import router as versions_router
from .config import Settings
from .diagnostics import Recorder
from .errors import DomainError

logger = logging.getLogger("ac")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = Container.build(settings)
        try:
            yield
        finally:
            app.state.container.shutdown()

    app = FastAPI(
        title="Aho-Corasick Streaming Matcher",
        version="1.0.0",
        description=(
            "Local backend: versioned byte-level AC automata, stream "
            "matching with raw-byte offsets, explicit-boundary version "
            "switching and resumable paginated hits."
        ),
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def diagnostic_mw(request: Request, call_next):
        request_id = resolve_request_id(request)
        rec = Recorder(request.app.state.container.diag_repo, request_id)
        request.state.recorder = rec

        content_length = request.headers.get("content-length")
        rec.record_request(
            method=request.method,
            path=request.url.path,
            state={
                "request_id": request_id,
                "content_length": int(content_length)
                if content_length and content_length.isdigit() else None,
                "query": str(request.url.query)[:200] or None,
            },
        )

        try:
            response = await call_next(request)
            response.headers["x-request-id"] = request_id
            return response
        except DomainError as exc:
            rec.record_decision(
                decision=exc.decision,
                code=exc.code,
                summary=f"{request.method} {request.url.path}: {exc.message}",
                method=request.method,
                path=request.url.path,
                state={"details": exc.details},
            )
            body = exc.to_dict()
            body["error"]["request_id"] = request_id
            return JSONResponse(
                body,
                status_code=exc.http_status,
                headers={"x-request-id": request_id},
            )
        except Exception as exc:  # noqa: BLE001 - defensive service boundary
            logger.exception("unhandled error req=%s", request_id)
            rec.record_decision(
                decision="inconclusive",
                code="internal_error",
                summary=(
                    f"{request.method} {request.url.path} failed with "
                    f"{type(exc).__name__}; result cannot be guaranteed"
                ),
                method=request.method,
                path=request.url.path,
            )
            return JSONResponse(
                {
                    "error": {
                        "code": "internal_error",
                        "message": "internal error; see diagnostics request id",
                        "decision": "inconclusive",
                        "request_id": request_id,
                    }
                },
                status_code=500,
                headers={"x-request-id": request_id},
            )

    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        rec: Recorder = request.state.recorder
        rec.record_decision(
            decision=exc.decision,
            code=exc.code,
            summary=f"{request.method} {request.url.path}: {exc.message}",
            method=request.method,
            path=request.url.path,
            # Details carry offsets/indices/lengths only — never byte contents.
            state={"details": exc.details},
        )
        body = exc.to_dict()
        body["error"]["request_id"] = rec.request_id
        return JSONResponse(
            body,
            status_code=exc.http_status,
            headers={"x-request-id": rec.request_id},
        )

    app.add_exception_handler(DomainError, domain_error_handler)

    app.include_router(versions_router)
    app.include_router(scans_router)
    app.include_router(diag_router)

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok", "service": "ac-streaming-matcher",
                "version": app.version}

    return app


app = create_app()
