"""FastAPI audit interface.

Endpoints
---------
* ``GET  /health``                     liveness + component status
* ``POST /api/v1/review``              review a template + bindings
* ``GET  /api/v1/audit/{request_id}``  fetch one audit record
* ``GET  /api/v1/audit``               list recent audit records
* ``GET  /api/v1/audit/chain/verify``  verify the HMAC hash chain
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException

from ..config import Settings
from ..core.policy import load_policy
from ..logging_setup import configure_logging
from ..service import ReviewService
from ..state.audit import AuditStore, load_or_create_key
from ..state.fixture import fixture_from_dir
from .schemas import (
    AuditListEntry,
    ChainReportBody,
    ReviewRequest,
    ReviewResponseBody,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    logger = configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        policy = load_policy(settings.policy_path)
        fixture_dir = Path(settings.fixture_dir)
        if (fixture_dir / "fixture.db").exists() or (fixture_dir / "schema.sql").exists():
            fixture = fixture_from_dir(fixture_dir)
        else:
            logger.warning("no fixture directory found; schema checks will be skipped",
                           extra={"path": str(fixture_dir)})
            fixture = None
        key = load_or_create_key(settings.audit_key_path)
        audit = AuditStore(settings.audit_db_path, key)
        app.state.service = ReviewService(policy, fixture, audit, logger)
        app.state.settings = settings
        try:
            yield
        finally:
            audit.close()
            if fixture is not None:
                fixture.close()

    app = FastAPI(
        title="SQLGuard — restricted SQL template & binding reviewer",
        version="1.0.0",
        description="Reviews parameterized SQL templates without executing them.",
        lifespan=lifespan,
    )

    def service() -> ReviewService:
        svc = getattr(app.state, "service", None)
        if svc is None:
            raise HTTPException(503, "service not initialized")
        return svc

    @app.get("/health")
    def health(svc: ReviewService = Depends(service)) -> dict[str, str]:
        chain = svc.chain_report()
        return {
            "status": "ok",
            "fixture": "attached" if svc.fixture is not None else "missing",
            "audit_chain": "ok" if chain["ok"] else "BROKEN",
        }

    @app.post("/api/v1/review", response_model=ReviewResponseBody)
    def review(body: ReviewRequest, svc: ReviewService = Depends(service)) -> dict:
        response = svc.review(
            template=body.template,
            params=body.params,
            slots=body.slots,
            inline_policy=body.policy_overrides,
            request_id=body.request_id,
        )
        return {"request_id": response.request_id, **response.result}

    @app.get("/api/v1/audit/{request_id}")
    def get_audit(request_id: str, svc: ReviewService = Depends(service)) -> dict:
        record = svc.fetch_audit(request_id)
        if record is None:
            raise HTTPException(404, f"unknown request_id {request_id}")
        return record

    @app.get("/api/v1/audit", response_model=list[AuditListEntry])
    def list_audit(limit: int = 50, svc: ReviewService = Depends(service)):
        return svc.list_audit(limit=min(max(limit, 1), 500))

    @app.get("/api/v1/audit/chain/verify", response_model=ChainReportBody)
    def verify_chain(svc: ReviewService = Depends(service)):
        return svc.chain_report()

    return app


app = create_app()
