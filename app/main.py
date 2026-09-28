"""FastAPI application factory.

Run locally::

    uvicorn app.main:app --reload
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import DEFAULT_SETTINGS, Settings
from .jobs.manager import JobManager
from .jobs.store import JobStore
from .api.routes import create_router


def create_app(
    settings: Settings | None = None,
    store: JobStore | None = None,
) -> FastAPI:
    settings = settings or DEFAULT_SETTINGS
    external_store = store is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Create the SQLite-backed store lazily at startup so merely importing
        # this module for the ASGI path (app.main:app) never touches disk.
        own_store = store if external_store else JobStore(settings.db_path)
        manager = JobManager(own_store, settings)
        app.state.settings = settings
        app.state.store = own_store
        app.state.manager = manager
        yield
        manager.shutdown()
        if not external_store:
            own_store.close()

    app = FastAPI(
        title="Local MPEG-TS Analyzer",
        version="1.0.0",
        description=(
            "Parses 188-byte MPEG Transport Streams: PAT/PMT tables, per-PID"
            " continuity checking and restricted PES reassembly. All data stays"
            " local; inputs are synthetic fixtures in the test suite."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(create_router())
    return app


app = create_app()
