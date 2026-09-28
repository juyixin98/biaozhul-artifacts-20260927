"""FastAPI application exposing the review/audit interface.

Endpoints:
* ``GET  /health``                  — liveness + basis info
* ``POST /api/v1/audit/reviews``    — review a SQL template
* ``GET  /api/v1/audit/reviews/{request_id}`` — fetch one stored review
* ``GET  /api/v1/audit/reviews``    — recent verdict metadata

Every response carries a request id (honoring ``X-Request-Id`` when given)
so log lines and audit rows can be correlated. Review bodies never echo raw
sensitive values — see :mod:`sqlguard.redaction`.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .isolation import SchemaUnavailable
from .models import ReviewRequest, ReviewResponse
from .redaction import new_request_id
from .service import build_services

logger = logging.getLogger("sqlguard")

CONFIG_ERROR_RESPONSE = {
    "verdict": "unanalyzable",
    "findings": [{
        "code": "SCHEMA_UNAVAILABLE",
        "severity": "error",
        "message": "review service is not configured with a readable fixture",
    }],
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="SQL Template Review Service",
        version="1.0.0",
        description="Reviews restricted SQL templates and parameter bindings "
                    "without executing them.",
    )
    try:
        services = build_services(settings)
        app.state.services = services
        ready = True
        init_error: str | None = None
    except (SchemaUnavailable, FileNotFoundError, OSError) as exc:
        app.state.services = None
        ready = False
        init_error = str(exc)
        logger.error("service started without ready fixture: %s", init_error)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = request.headers.get("X-Request-Id", new_request_id())
        logger.exception("unhandled error request=%s", rid)
        return JSONResponse(
            status_code=500,
            content={
                "verdict": "unanalyzable",
                "request_id": rid,
                "findings": [{
                    "code": "INTERNAL_ERROR",
                    "severity": "error",
                    "message": "internal error during review",
                }],
                "diagnostics": {"request_id": rid,
                                "state": "redacted-internal-error"},
            },
        )

    @app.get("/health")
    async def health() -> dict:
        if not ready:
            return {"status": "degraded", "ready": False,
                    "reason": init_error}
        svc = app.state.services
        return {
            "status": "ok",
            "ready": True,
            "policy_id": svc.kernel.policy.policy_id,
            "schema_digest": svc.kernel.schema.digest,
            "tables": sorted(svc.kernel.schema.tables),
        }

    @app.post("/api/v1/audit/reviews", response_model=ReviewResponse)
    async def create_review(
        body: ReviewRequest,
        x_request_id: str | None = Header(default=None),
    ) -> ReviewResponse:
        if not ready:
            raise HTTPException(status_code=503, detail={
                "code": "SCHEMA_UNAVAILABLE",
                "message": init_error,
            })
        svc = app.state.services
        rid = x_request_id or new_request_id()
        result = svc.kernel.review(
            body.sql,
            parameters=body.parameters,
            identifiers=body.identifiers,
            request_id=rid,
        )
        payload = result.to_dict()
        stored = svc.store.record(payload)
        payload["stored"] = stored
        logger.info(
            "review request=%s verdict=%s reasons=%s sql_digest=%s",
            rid, payload["verdict"],
            [f["code"] for f in payload["findings"]],
            payload["sql_digest"],
        )
        return ReviewResponse(**payload)

    @app.get("/api/v1/audit/reviews/{request_id}")
    async def get_review(request_id: str) -> dict:
        if not ready:
            raise HTTPException(status_code=503, detail="not configured")
        record = app.state.services.store.fetch(request_id)
        if record is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "request_id": request_id,
            })
        return record

    @app.get("/api/v1/audit/reviews")
    async def list_reviews(limit: int = 20) -> dict:
        if not ready:
            raise HTTPException(status_code=503, detail="not configured")
        return {"records": app.state.services.store.recent_verdicts(limit)}

    return app


app = create_app()
