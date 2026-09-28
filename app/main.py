"""FastAPI application assembly (audit interface layer)."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import meta_router, router
from app.config import Settings, get_settings
from app.observability import configure_logging
from app.storage.db import Database
from app.version import __version__


def create_app(settings: Settings | None = None, *, configure_logs: bool = True) -> FastAPI:
    settings = settings or get_settings()
    if configure_logs:
        configure_logging(settings.audit_log_path)
    db = Database(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            db.close()

    app = FastAPI(
        title="Local Audit Batch Commitments",
        version=__version__,
        lifespan=lifespan,
        description="Field-level commitments and selective-disclosure verification "
                    "for local audit batches (synthetic data only).",
    )
    app.state.settings = settings
    app.state.db = db
    app.include_router(meta_router)
    app.include_router(router)
    return app


app = create_app()
