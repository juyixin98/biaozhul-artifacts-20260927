"""FastAPI application factory and runnable entrypoint.

Run locally::

    uvicorn app.main:app --reload
    python -m app.main
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api.errors import ApiError, error_envelope
from .api.routes import router
from .config import Settings, get_settings
from .diagnostics import new_request_id
from .logging_setup import configure_logging, get_logger
from .storage.registry import VersionRegistry
from .storage.repository import DictionaryRepository

logger = get_logger("app")


def load_seed(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data["entries"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)
    repo = DictionaryRepository(settings.db_path)
    registry = VersionRegistry(
        repo,
        min_word_cost=settings.min_word_cost,
        unknown_char_cost=settings.unknown_char_cost,
        max_word_length=settings.max_word_length,
    )
    app.state.registry = registry
    if settings.auto_seed:
        try:
            seeded = registry.ensure_seeded(load_seed(settings.seed_path))
            if seeded is not None:
                logger.info("seeded initial dictionary version=%s words=%d",
                            seeded.version, seeded.word_count())
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("seed dictionary unusable: %s", exc)
    current = registry.current()
    logger.info("service ready current_version=%s", current.version if current else None)
    yield
    logger.info("service shutting down")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Optimal Dictionary Segmentation API",
        version="1.0.0",
        description="DAG shortest-path segmentation with versioned dictionaries.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(router)

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        request_id = exc.request_id or new_request_id()
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(exc.code, exc.message, request_id, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = new_request_id()
        # Keep only structural error info -- never echo raw bodies wholesale.
        safe_errors = []
        for err in exc.errors():
            safe_errors.append({
                "location": [str(p) for p in err.get("loc", [])],
                "code": err.get("type", "validation_error"),
                "message": err.get("msg", ""),
            })
        return JSONResponse(
            status_code=422,
            content=error_envelope("invalid_request",
                                   "request payload failed schema validation",
                                   request_id, safe_errors),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = new_request_id()
        logger.exception("unhandled error request_id=%s", request_id)
        return JSONResponse(
            status_code=500,
            content=error_envelope("internal_error",
                                   "unexpected internal failure", request_id),
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
