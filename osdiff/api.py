"""FastAPI audit/query interface.

There is deliberately no user/role management surface: callers are identified
only by an opaque, caller-supplied ``client_ref`` echoed back for correlation.
Endpoints expose analysis runs, witnesses, verification and the audit trail;
failures and uncertainty (UNKNOWN verdicts / POSSIBLE expansion) are presented
in dedicated fields rather than folded into success responses.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import load_config
from .policy import PolicyParseError
from .service import Service, ServiceError
from .types import Failure


class DiffRequest(BaseModel):
    old_policy: dict[str, Any]
    new_policy: dict[str, Any]
    run_id: str | None = None
    client_ref: str | None = Field(default=None, description="opaque caller correlation id, echoed back")


class VerifyRequest(BaseModel):
    policy: dict[str, Any] | None = None
    version_id: str | None = None
    request: dict[str, Any]
    expected_verdict: str | None = None
    client_ref: str | None = None


class ErrorBody(BaseModel):
    ok: bool = False
    failure_code: str
    message: str
    details: Any = None
    client_ref: str | None = None


def create_app(service: Service) -> FastAPI:
    app = FastAPI(
        title="osdiff — object-storage policy differential analysis",
        version="0.1.0",
        docs_url="/docs",
    )

    def get_service() -> Service:
        return service

    @app.exception_handler(ServiceError)
    async def _service_error_handler(_request: Any, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"ok": False, "failure_code": exc.code.value,
                     "message": exc.message, "details": exc.details},
        )

    @app.exception_handler(PolicyParseError)
    async def _parse_error_handler(_request: Any, exc: PolicyParseError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "failure_code": Failure.PARSE_ERROR.value,
                     "message": str(exc), "details": {"errors": exc.errors}},
        )

    @app.get("/health")
    def health(svc: Service = Depends(get_service)) -> dict[str, Any]:
        return {"ok": True, "config": svc.config.to_dict()}

    @app.post("/v1/diff")
    def diff(body: DiffRequest, svc: Service = Depends(get_service)) -> dict[str, Any]:
        result = svc.run_diff(body.old_policy, body.new_policy, run_id=body.run_id)
        summary = result.summary()
        return {
            "ok": True,
            "client_ref": body.client_ref,
            "run_id": result.run_id,
            "conclusion": {
                "expands_proven_access": result.expands,
                "possibly_expands": result.possibly_expands,
                "contracts_proven_access": result.contracts,
            },
            "uncertainty": {
                "note": "UNKNOWN means conditions reference unknown values; it is never treated as allow.",
                "counts": {
                    "newly_unknown_after_denied": summary["counts"]["EXPANSION_POSSIBLE"],
                    "unknown_resolved_to_allow": summary["counts"]["RESOLVED_UNCERTAINTY"],
                    "unknown_resolved_to_deny": summary["counts"]["REDUCED_POSSIBLE"],
                    "unknown_in_both": summary["counts"]["UNCHANGED"],
                },
            },
            "result": summary,
        }

    @app.get("/v1/runs")
    def list_runs(svc: Service = Depends(get_service)) -> dict[str, Any]:
        return {"ok": True, "runs": svc.list_runs()}

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, svc: Service = Depends(get_service)) -> dict[str, Any]:
        return {"ok": True, **svc.get_run(run_id)}

    @app.get("/v1/runs/{run_id}/witnesses")
    def get_witnesses(
        run_id: str,
        category: str | None = Query(default=None, description="filter by transition category"),
        svc: Service = Depends(get_service),
    ) -> dict[str, Any]:
        return {"ok": True, "run_id": run_id, "category": category,
                "witnesses": svc.get_witnesses(run_id, category)}

    @app.post("/v1/verify")
    def verify(body: VerifyRequest, svc: Service = Depends(get_service)) -> dict[str, Any]:
        versions: tuple[str, str] | None = (body.version_id, "") if body.version_id else None
        out = svc.verify_request(body.policy, body.request, expected_verdict=body.expected_verdict,
                                 use_versions=versions)
        out["ok"] = True
        out["client_ref"] = body.client_ref
        return out

    @app.post("/v1/runs/{run_id}/verify-signature")
    def verify_run_sig(run_id: str, svc: Service = Depends(get_service)) -> dict[str, Any]:
        return {"ok": True, **svc.verify_run_signature(run_id)}

    @app.get("/v1/audit")
    def audit(
        run_id: str | None = None,
        request_id: str | None = None,
        svc: Service = Depends(get_service),
    ) -> dict[str, Any]:
        events = svc.get_audit(run_id=run_id, request_id=request_id)
        return {"ok": True, "run_id": run_id, "request_id": request_id,
                "count": len(events), "events": events}

    @app.post("/v1/audit/verify")
    def audit_verify(svc: Service = Depends(get_service)) -> dict[str, Any]:
        return {"ok": True, **svc.verify_audit_chain()}

    return app


def build_default_app() -> FastAPI:
    return create_app(Service(load_config()))
