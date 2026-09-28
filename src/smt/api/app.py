"""FastAPI application factory and error middleware."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..config import Settings, get_settings
from ..observability.diagnostics import configure_logging, event, new_request_id
from ..services.state_service import StateService
from ..storage.node_store import SqliteNodeStore
from .routes import router
from .state import AppState


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logger = configure_logging(level=settings.log_level, fmt=settings.log_format)
    store = SqliteNodeStore(settings.sqlite_path)
    service = StateService(store, hmac_key=settings.journal_hmac_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.deps = AppState(settings=settings, service=service, logger=logger)
        try:
            yield
        finally:
            store.close()

    app = FastAPI(
        title="Fixed-key-width sparse Merkle state service",
        version="1.0.0",
        description="Updates, membership and non-membership proofs over a "
        "256-bit-key sparse Merkle trie (spec smt-v1).",
        lifespan=lifespan,
    )
    app.state.deps = AppState(settings=settings, service=service, logger=logger)
    app.include_router(router)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or new_request_id()
        request.state.request_id = rid
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - last-resort boundary
            event(
                logger, logging.ERROR, "unhandled exception", rid,
                path=request.url.path, method=request.method,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "request_id": rid,
                    "error": {
                        "category": "internal_error",
                        "message": "internal error; correlate with request_id",
                    },
                },
            )
        response.headers["x-request-id"] = rid
        return response

    return app


def main() -> None:
    """Run with: python -m smt.api.app (uses uvicorn)."""
    import uvicorn

    settings = get_settings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
