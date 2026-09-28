"""FastAPI application factory.

The service factory is a closure over the run-bound logger so every request
gets its own :class:`CommitmentService` carrying its ``run_id``.
"""
from __future__ import annotations

from fastapi import FastAPI

from app import SERVICE_NAME, SERVICE_VERSION
from app.api.routes import router
from app.audit.logging_config import configure_logging
from app.config import PROTOCOL_VERSION, Settings
from app.security.saltpolicy import SaltPolicy
from app.service import CommitmentService
from app.storage import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()  # type: ignore[call-arg]
    logger = configure_logging(settings)
    db = Database(settings.db_path)
    policy = SaltPolicy(
        digest_name=settings.digest.value,
        salt_bytes=settings.default_salt_bytes,
        allow_unsalted=settings.allow_unsalted,
    )

    app = FastAPI(
        title=SERVICE_NAME,
        version=SERVICE_VERSION,
        description=(
            "Local audit batches with field-level commitments and "
            "selective disclosure verification."
        ),
    )
    app.state.settings = settings
    app.state.db = db
    app.state.logger = logger
    app.state.service_factory = lambda run_log: CommitmentService(db, policy, run_log)
    app.include_router(router)

    logger.info(
        "application started service=%s version=%s protocol=%s db=%s digest=%s",
        SERVICE_NAME,
        SERVICE_VERSION,
        PROTOCOL_VERSION,
        settings.db_path,
        settings.digest.value,
    )
    return app


app = create_app()
