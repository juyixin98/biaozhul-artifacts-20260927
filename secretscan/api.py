"""HTTP audit interface (FastAPI).

Every response is wrapped in an envelope carrying the request identity, so a
caller can correlate an API result with the persisted audit events. Error
responses use the same shape and expose the stable failure code.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .errors import SecretscanError
from .idutils import new_request_id
from .logging_setup import get_logger
from .redact import Redactor
from .report import build_report, render_markdown
from .service import ScanService
from .storage import Store

log = get_logger()


class ScanRequest(BaseModel):
    root: str = Field(..., description="Absolute path of the local snapshot directory")
    actor: str | None = None
    note: str = ""


class BaselineRequest(BaseModel):
    rule_id: str
    fingerprint: str
    note: str = ""
    actor: str | None = None


class BaselineRevokeRequest(BaseModel):
    rule_id: str
    fingerprint: str
    actor: str | None = None


class TriageRequest(BaseModel):
    rule_id: str
    fingerprint: str
    triage: str
    actor: str | None = None


def _identity(request: Request, body_actor: str | None = None) -> tuple[str, str]:
    actor = (
        body_actor
        or request.headers.get("x-actor")
        or request.headers.get("x-user")
        or "anonymous"
    )
    request_id = request.headers.get("x-request-id") or new_request_id()
    return actor, request_id


def _envelope(request_id: str, payload: dict, *, ok: bool = True, code: str | None = None) -> dict:
    env = {"ok": ok, "request_id": request_id}
    if code:
        env["error"] = {"code": code, "message": payload.get("message", "")}
    else:
        env["data"] = payload
    return env


def create_app(state_dir: str | Path, config_path: str | Path) -> FastAPI:
    app = FastAPI(
        title="offline-secret-scan",
        version="1.0.0",
        description="Offline repository snapshot secret candidate scanning.",
    )
    store = Store(state_dir)
    service = ScanService(store, config_path)
    redactor = Redactor()
    app.state.store = store
    app.state.service = service

    @app.middleware("http")
    async def correlate_and_redact(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or new_request_id()
        log.info(
            "api request",
            extra={
                "request_id": request_id,
                "actor": request.headers.get("x-actor", "anonymous"),
                "step": "request",
            },
        )
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("application/json"):
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
            safe = redactor.redact(body.decode("utf-8", errors="replace"))
            return JSONResponse(
                content=json.loads(safe),
                status_code=response.status_code,
                headers={
                    **{k: v for k, v in response.headers.items() if k.lower() != "content-length"},
                    "x-request-id": request_id,
                },
            )
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(SecretscanError)
    async def expected_error_handler(request: Request, exc: SecretscanError):
        actor, request_id = _identity(request)
        log.warning(
            "api expected error: %s",
            exc.message,
            extra={"request_id": request_id, "actor": actor, "code": exc.code, "step": "error"},
        )
        store.append_registry_audit(
            request_id=request_id,
            actor=actor,
            action="api.error",
            project_id=project_from_request(request),
            detail={"code": exc.code, "message": exc.message, "path": request.url.path},
        )
        status = {
            "not_found": 404,
            "validation_error": 400,
            "root_invalid": 400,
            "config_invalid": 500,
            "state_conflict": 409,
        }.get(exc.code, 400)
        return JSONResponse(
            _envelope(request_id, {"message": exc.message}, ok=False, code=exc.code),
            status_code=status,
        )

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "rules_version": service._ruleset.rules_version,
            "classification_version": service._ruleset.classification_version,
        }

    @app.post("/projects/scan")
    async def scan(scan_req: ScanRequest, request: Request):
        actor, request_id = _identity(request, scan_req.actor)
        result = service.run_scan(
            scan_req.root, actor=actor, request_id=request_id, note=scan_req.note
        )
        return _envelope(request_id, {
            "project_id": result["project_id"],
            "scan_id": result["scan_id"],
            "summary": result["summary"],
            "rules_version": result["rules_version"],
            "config_digest": result["config_digest"],
        })

    @app.get("/projects/{project_id}")
    async def project(project_id: str, request: Request):
        actor, request_id = _identity(request)
        project = store.get_project(project_id)
        if project is None:
            from .errors import NotFoundError
            raise NotFoundError(f"unknown project_id {project_id}")
        return _envelope(request_id, project)

    @app.get("/projects/{project_id}/scans")
    async def scans(project_id: str, request: Request, limit: int = 50):
        _, request_id = _identity(request)
        return _envelope(request_id, {"scans": service.list_scans(project_id, limit=limit)})

    @app.get("/projects/{project_id}/scans/{scan_id}")
    async def scan_detail(project_id: str, scan_id: str, request: Request):
        _, request_id = _identity(request)
        return _envelope(request_id, service.get_scan(project_id, scan_id))

    @app.get("/projects/{project_id}/scans/{scan_id}/report")
    async def scan_report(project_id: str, scan_id: str, request: Request, format: str = "json"):
        _, request_id = _identity(request)
        result = service.get_scan(project_id, scan_id)
        report = build_report(result)
        if format == "md":
            return PlainTextResponse(
                render_markdown(report), headers={"x-request-id": request_id}
            )
        return _envelope(request_id, report)

    @app.get("/projects/{project_id}/candidates")
    async def candidates(project_id: str, request: Request, state: str | None = None):
        _, request_id = _identity(request)
        return _envelope(request_id, {
            "candidates": service.list_candidates(project_id, state=state)
        })

    @app.get("/projects/{project_id}/audit")
    async def audit(project_id: str, request: Request, limit: int = 100):
        _, request_id = _identity(request)
        return _envelope(request_id, {"events": service.audit_trail(project_id, limit=limit)})

    @app.get("/projects/{project_id}/baseline")
    async def baseline_list(project_id: str, request: Request):
        _, request_id = _identity(request)
        return _envelope(request_id, {"exemptions": service.list_baseline(project_id)})

    @app.post("/projects/{project_id}/baseline")
    async def baseline_accept(
        project_id: str, req: BaselineRequest, request: Request
    ):
        actor, request_id = _identity(request, req.actor)
        out = service.accept_baseline(
            project_id,
            rule_id=req.rule_id,
            fingerprint=req.fingerprint,
            actor=actor,
            request_id=request_id,
            note=req.note,
        )
        return _envelope(request_id, out)

    @app.delete("/projects/{project_id}/baseline")
    async def baseline_revoke(
        project_id: str, req: BaselineRevokeRequest, request: Request
    ):
        actor, request_id = _identity(request, req.actor)
        out = service.revoke_baseline(
            project_id,
            rule_id=req.rule_id,
            fingerprint=req.fingerprint,
            actor=actor,
            request_id=request_id,
        )
        return _envelope(request_id, out)

    @app.post("/projects/{project_id}/candidates/triage")
    async def triage(project_id: str, req: TriageRequest, request: Request):
        actor, request_id = _identity(request, req.actor)
        out = service.triage_candidate(
            project_id,
            rule_id=req.rule_id,
            fingerprint=req.fingerprint,
            triage=req.triage,
            actor=actor,
            request_id=request_id,
        )
        return _envelope(request_id, out)

    return app


def project_from_request(request: Request) -> str:
    try:
        return request.path_params["project_id"]
    except KeyError:
        return "-"
