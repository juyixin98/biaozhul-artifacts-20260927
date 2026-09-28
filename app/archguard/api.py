"""FastAPI HTTP surface.

Endpoints
---------
``POST /api/v1/inspect``     multipart upload of one archive -> verdict
``GET  /api/v1/runs``        recent runs
``GET  /api/v1/runs/{id}``   one run's stored result
``GET  /api/v1/runs/{id}/events``   correlated audit events for a run
``GET  /api/v1/audit/verify``       recompute the JSONL hash chain
``GET  /api/v1/manifests/{id}``     signed manifest envelope
``GET  /healthz``            liveness + version + counters
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from . import __version__
from .audit import verify_chain
from .config import Config
from .engine import Engine
from .errors import HTTP_STATUS, RejectionCategory
from .isolation import ensure_home
from .store import Store
from .audit import AuditLogger


def create_app(config: Config | None = None) -> FastAPI:
    if config is None:
        config = Config.load()
    ensure_home(config.home)
    store = Store(config.home)
    audit = AuditLogger(config.home)
    audit.set_sink(store.record_event)
    engine = Engine(config, store, audit)

    app = FastAPI(
        title="archguard",
        version=__version__,
        description="Local archive inspection and controlled extraction service",
    )
    app.state.config = config
    app.state.store = store
    app.state.audit = audit
    app.state.engine = engine

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "archguard",
            "version": __version__,
            "home": str(config.home),
            "budgets": engine.budget.to_dict(),
            "counters": store.stats(),
        }

    @app.post("/api/v1/inspect")
    async def inspect(
        request: Request,
        file: UploadFile = File(..., description="ZIP or uncompressed TAR"),
    ) -> JSONResponse:
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > config.max_upload_bytes:
                    return _rejected_response(
                        RejectionCategory.UPLOAD_LIMIT,
                        f"Content-Length {declared} exceeds limit "
                        f"{config.max_upload_bytes}",
                        None,
                        run_id=None,
                        http_override=413,
                    )
            except ValueError:
                pass

        data = await file.read(config.max_upload_bytes + 1)
        verdict = engine.inspect(data, input_name=file.filename or "upload.bin")
        status_code = 200 if verdict.accepted else HTTP_STATUS.get(
            RejectionCategory(verdict.category)
            if verdict.category
            else RejectionCategory.INTERNAL_ERROR,
            422,
        )
        return JSONResponse(verdict.to_dict(), status_code=status_code)

    @app.get("/api/v1/runs")
    def list_runs(limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(limit, 200))
        return {"runs": store.list_runs(limit)}

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        row = store.get_run(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown run id")
        return row

    @app.get("/api/v1/runs/{run_id}/events")
    def get_events(run_id: str) -> dict[str, Any]:
        if store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="unknown run id")
        return {"run_id": run_id, "events": store.get_events(run_id)}

    @app.get("/api/v1/audit/verify")
    def audit_verify() -> dict[str, Any]:
        return verify_chain(audit.log_path)

    @app.get("/api/v1/manifests/{run_id}")
    def get_manifest(run_id: str) -> dict[str, Any]:
        path = config.home / "manifests" / f"{run_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="manifest not found")
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    return app


def _rejected_response(
    category: RejectionCategory,
    detail: str,
    entry: str | None,
    *,
    run_id: str | None,
    http_override: int | None = None,
) -> JSONResponse:
    return JSONResponse(
        {
            "accepted": False,
            "run_id": run_id,
            "status": "rejected",
            "failure": {"category": category.value, "detail": detail, "entry": entry},
            "version": __version__,
        },
        status_code=http_override or HTTP_STATUS.get(category, 422),
    )
