"""Local audit HTTP interface (FastAPI).

The API is intentionally small and explainable:

* ``POST /scans``            trigger a scan (only within configured roots)
* ``GET  /scans/{id}``       one full report (masks/fingerprints only)
* ``GET  /scans/{id}/findings``  findings filterable by lifecycle state
* ``GET  /scans``            scan history
* ``GET  /findings/{id}``    single finding with all stored occurrences
* ``GET  /audit``            audit trail, filterable by request id / action
* ``GET  /healthz``          service identity + loaded config versions

Every response carries the request id (echoed from ``X-Request-Id`` or
generated) and the actor id (``X-Actor-Id``). Failure reasons and uncertain
conclusions are separate sections of the report — they are never folded into
"passed". Raw secrets are never present in any response body.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from . import audit, service, state
from .baseline import Baseline
from .config import RulePack, ScopePack, Settings
from .security import Fingerprinter

STATES = (service.STATE_NEW, service.STATE_OPEN, service.STATE_MOVED,
          service.STATE_KNOWN_FIXED, service.STATE_UNCERTAIN_REMOVAL,
          service.STATE_BASELINE_EXEMPT)


class ScanRequest(BaseModel):
    root: str


def _error(code: str, message: str, request_id: str, status: int = 400,
           **extra) -> HTTPException:
    detail = {"code": code, "message": message, "request_id": request_id}
    detail.update(extra)
    return HTTPException(status_code=status, detail=detail)


def create_app(settings: Settings, rules: RulePack, scope: ScopePack,
               baseline: Baseline | None = None) -> FastAPI:
    """Build the FastAPI application bound to one isolated workspace."""
    conn = state.connect(settings.workspace_db)
    fingerprinter = Fingerprinter(settings.fingerprint_pepper)
    logger, redactor = audit.configure_logging(settings.log_file)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        conn.close()

    app = FastAPI(
        title="offline secret-candidate scanner",
        version="1.0.0",
        description="Read-mostly local audit API; candidates, never confirmed "
                    "leaks; fully offline.",
        lifespan=lifespan)

    def ctx(request_id: str | None, actor_id: str | None) -> audit.RequestContext:
        return audit.RequestContext.create(request_id, actor_id)

    @app.get("/healthz")
    def healthz(x_request_id: str | None = Header(default=None),
                x_actor_id: str | None = Header(default=None)) -> dict:
        context = ctx(x_request_id, x_actor_id)
        with context:
            return {
                "status": "ok",
                "request_id": context.request_id,
                "actor_id": context.actor_id,
                "offline": True,
                "versions": {
                    "rule_pack": rules.fingerprint(),
                    "scope_pack": scope.fingerprint(),
                    "fingerprint_pepper_id": fingerprinter.pepper_id,
                    "baseline": str(baseline.path) if baseline else None,
                }}

    @app.post("/scans", status_code=201)
    def trigger_scan(
        body: ScanRequest,
        x_request_id: str | None = Header(default=None),
        x_actor_id: str | None = Header(default=None),
    ) -> dict:
        context = ctx(x_request_id, x_actor_id)
        root = Path(body.root)
        if not root.is_absolute():
            raise _error("root_not_absolute",
                         "scan root must be an absolute path",
                         context.request_id)
        if not root.resolve().exists():
            raise _error("root_not_found", "scan root does not exist",
                         context.request_id, status=404)
        svc = service.ScanService(
            conn, rules, scope, fingerprinter, logger, baseline)
        # NB: do NOT bind ``context`` here — run_scan binds it itself, and
        # re-entering the SAME RequestContext object would reset contextvar
        # tokens out of order. Request id/actor are still on the object for
        # error bodies.
        try:
            report = svc.run_scan(
                root, context, allowed_roots=settings.allowed_roots)
        except PermissionError as exc:
            # Persist + log the denial (no scan transaction was opened), then
            # return a structured 403. Fresh short binding for the log line.
            with audit.RequestContext.create(context.request_id,
                                             context.actor_id):
                from datetime import datetime, timezone
                state.insert_audit(
                    conn, scan_id=None,
                    ts=datetime.now(timezone.utc).isoformat(),
                    actor_id=context.actor_id, request_id=context.request_id,
                    action=audit.ACT_API_DENIED, target_type="root",
                    target=str(root),
                    details={"reason_code": "root_not_allowed"},
                    outcome="denied")
                conn.commit()
                audit.audit_event(
                    logger, action=audit.ACT_API_DENIED, target_type="root",
                    target=str(root), outcome="denied",
                    reason_code="root_not_allowed")
            raise _error("root_not_allowed", str(exc),
                         context.request_id, status=403) from exc
        except NotADirectoryError as exc:
            raise _error("root_not_a_directory", str(exc),
                         context.request_id, status=400) from exc
        return {"request_id": context.request_id,
                "actor_id": context.actor_id,
                "report": report.to_dict()}

    @app.get("/scans")
    def list_scans(limit: int = Query(default=50, ge=1, le=500),
                   x_request_id: str | None = Header(default=None),
                   x_actor_id: str | None = Header(default=None)) -> dict:
        context = ctx(x_request_id, x_actor_id)
        with context:
            rows = conn.execute(
                "SELECT id, started_at, finished_at, root, status, "
                "rule_pack_version, scope_pack_version, request_id, actor_id "
                "FROM scans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return {"request_id": context.request_id,
                    "scans": [dict(r) for r in rows]}

    @app.get("/scans/{scan_id}")
    def get_scan(scan_id: int,
                 x_request_id: str | None = Header(default=None),
                 x_actor_id: str | None = Header(default=None)) -> dict:
        context = ctx(x_request_id, x_actor_id)
        with context:
            report = service.load_report(conn, scan_id)
            if report is None:
                raise _error("scan_not_found", f"no scan with id {scan_id}",
                             context.request_id, status=404)
            return {"request_id": context.request_id, "report": report}

    @app.get("/scans/{scan_id}/findings")
    def get_scan_findings(
        scan_id: int,
        lifecycle: str | None = Query(default=None),
        x_request_id: str | None = Header(default=None),
        x_actor_id: str | None = Header(default=None),
    ) -> dict:
        context = ctx(x_request_id, x_actor_id)
        with context:
            report = service.load_report(conn, scan_id)
            if report is None:
                raise _error("scan_not_found", f"no scan with id {scan_id}",
                             context.request_id, status=404)
            if lifecycle is not None and lifecycle not in STATES:
                raise _error(
                    "unknown_lifecycle",
                    f"lifecycle must be one of {list(STATES)}",
                    context.request_id)
            findings = report["findings"]
            if lifecycle is not None:
                selected = {lifecycle: findings.get(lifecycle, [])}
            else:
                selected = findings
            return {"request_id": context.request_id, "scan_id": scan_id,
                    "lifecycle_filter": lifecycle, "findings": selected}

    @app.get("/findings/{finding_id}")
    def get_finding(finding_id: int,
                    x_request_id: str | None = Header(default=None),
                    x_actor_id: str | None = Header(default=None)) -> dict:
        context = ctx(x_request_id, x_actor_id)
        with context:
            finding = conn.execute(
                "SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
            if finding is None:
                raise _error("finding_not_found",
                             f"no finding with id {finding_id}",
                             context.request_id, status=404)
            occurrences = conn.execute(
                "SELECT scan_id, relpath, line, column, end_line, end_column, "
                "evidence_masked, entropy, content_media, file_sha256, "
                "file_size, exempt FROM occurrences WHERE finding_id=? "
                "ORDER BY scan_id, relpath",
                (finding_id,)).fetchall()
            return {
                "request_id": context.request_id,
                "finding": {
                    "id": finding["id"], "fingerprint": finding["fingerprint"],
                    "rule_id": finding["rule_id"], "mask": finding["mask"],
                    "state": finding["state"],
                    "confidence": finding["confidence"],
                    "first_scan_id": finding["first_scan_id"],
                    "latest_scan_id": finding["latest_scan_id"],
                    "first_seen_at": finding["first_seen_at"],
                    "latest_seen_at": finding["latest_seen_at"],
                    "occurrences": [{
                        **{k: o[k] for k in o.keys() if k != "evidence_masked"},
                        "evidence": o["evidence_masked"],
                        "exempt": bool(o["exempt"]),
                    } for o in occurrences]}}

    @app.get("/audit")
    def get_audit(scan_id: int | None = Query(default=None),
                  request: str | None = Query(default=None),
                  action: str | None = Query(default=None),
                  limit: int = Query(default=100, ge=1, le=1000),
                  x_request_id: str | None = Header(default=None),
                  x_actor_id: str | None = Header(default=None)) -> dict:
        context = ctx(x_request_id, x_actor_id)
        with context:
            sql = ("SELECT id, scan_id, ts, actor_id, request_id, action, "
                   "target_type, target, details_json, outcome "
                   "FROM audit_events WHERE 1=1")
            params: list = []
            if scan_id is not None:
                sql += " AND scan_id=?"
                params.append(scan_id)
            if request is not None:
                sql += " AND request_id=?"
                params.append(request)
            if action is not None:
                sql += " AND action=?"
                params.append(action)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            import json as _json
            return {"request_id": context.request_id,
                    "events": [{**dict(r),
                                "details": _json.loads(r["details_json"])}
                               for r in rows]}

    return app
