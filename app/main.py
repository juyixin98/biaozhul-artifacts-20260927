"""FastAPI application factory and entrypoint.

Run locally:
    .venv/bin/uvicorn app.main:app --reload
or:
    .venv/bin/python -m app.main
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from . import __version__
from .api.routes import router
from .audit.audit import AuditDB
from .config import Settings, load_settings
from .service import GuardService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.audit = AuditDB(settings.audit_db, version=settings.version)
        app.state.service = GuardService(settings, app.state.audit)
        try:
            yield
        finally:
            app.state.audit.close()

    app = FastAPI(
        title="ArchiveGuard",
        version=__version__,
        description=(
            "Local archive security inspection and controlled extraction "
            "(restricted ZIP/TAR subset)."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(router)
    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
