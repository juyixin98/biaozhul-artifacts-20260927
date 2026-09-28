"""FastAPI surface for the audit backend.

Error mapping is explicit: the kernel/service error taxonomy becomes
HTTP status codes without leaking internal tracebacks.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import AuditError
from .parsing import parse_policy
from .service import AuditService

_DEFAULT_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "var" / "audit.db"


class PolicyValidateRequest(BaseModel):
    policy: dict[str, Any]


class AuditRequest(BaseModel):
    policy: dict[str, Any]
    evidence: dict[str, Any] | None = None
    fixture: str | None = None
    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def create_app(
    *,
    db_path: str | Path | None = None,
    fixtures_dir: str | Path | None = None,
    key_dir: str | Path | None = None,
) -> FastAPI:
    service = AuditService(
        db_path or _DEFAULT_DB,
        fixtures_dir=fixtures_dir or _DEFAULT_FIXTURES,
        key_dir=key_dir,
    )

    app = FastAPI(
        title="cache-key-vary-audit",
        version="0.1.0",
        description="Audits local response-metadata cache keys and Vary policy.",
    )
    app.state.service = service

    @app.exception_handler(AuditError)
    async def _audit_error_handler(_request, exc: AuditError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "key_id": service.key_id}

    @app.post("/policies/validate")
    async def validate_policy(req: PolicyValidateRequest) -> dict[str, Any]:
        # Raising InputError is handled by the audit error handler.
        policy = parse_policy(req.policy)
        return {
            "valid": True,
            "policy": {
                "name": policy.name,
                "covered_dimensions": list(policy.covered_dimensions),
                "identity_mode": policy.identity_mode,
                "shared": policy.shared,
                "max_requests": policy.max_requests,
                "max_findings": policy.max_findings,
            },
        }

    @app.post("/audits", status_code=201)
    async def create_audit(req: AuditRequest) -> dict[str, Any]:
        return service.run_audit(
            req.policy,
            evidence=req.evidence,
            fixture=req.fixture,
            run_id=req.run_id,
        )

    @app.get("/audits/{run_id}")
    async def get_audit(run_id: str) -> dict[str, Any]:
        return service.get_report(run_id)

    @app.get("/audits/{run_id}/events")
    async def get_audit_events(run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "events": service.get_events(run_id)}

    @app.get("/audits/{run_id}/verify")
    async def verify_audit(run_id: str) -> dict[str, Any]:
        return service.verify_report(run_id)

    return app


app = create_app()
