"""FastAPI HTTP boundary.

Endpoints
---------
POST /v1/merge/validate   build and return the action plan; no data change
POST /v1/merge            validate then atomically commit
GET  /v1/runs             recent runs
GET  /v1/runs/{run_id}    one run (status, spec, counts, error)
GET  /v1/runs/{run_id}/actions   planned/committed action set
GET  /v1/runs/{run_id}/traces    per-source-row decision trace
GET  /v1/runs/{run_id}/snapshot  full replay inputs (source + target)
GET  /healthz            liveness

Error envelope (every MergeError, no stack traces leaked)::

    {"error": {"category": "...", "code": "...", "message": "...",
               "details": {...}}, "run_id": "..."}

Category -> HTTP status:
    INPUT_ERROR        400
    STATE_CONFLICT     409
    RESOURCE_EXHAUSTED 503
    COMPUTATION_FAILURE 422
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import Category, MergeError
from .service import MergeService

_STATUS = {
    Category.INPUT_ERROR: 400,
    Category.STATE_CONFLICT: 409,
    Category.RESOURCE_EXHAUSTED: 503,
    Category.COMPUTATION_FAILURE: 422,
}


def create_app(service: MergeService) -> FastAPI:
    app = FastAPI(
        title="Composite-Key MERGE Decision Engine",
        version="1.0.0",
        description="Bounded MERGE backend with validate-before-atomic-commit semantics.",
    )
    app.state.service = service

    @app.exception_handler(MergeError)
    async def _merge_error_handler(_request: Request, exc: MergeError) -> JSONResponse:
        body = {"error": exc.to_dict()}
        run_id = getattr(exc, "_run_id", None)
        if run_id:
            body["run_id"] = run_id
        return JSONResponse(status_code=_STATUS[exc.category], content=body)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/merge/validate")
    async def validate(request: Request) -> dict[str, Any]:
        payload = await request.json()
        return service.validate(payload)

    @app.post("/v1/merge")
    async def merge(request: Request) -> dict[str, Any]:
        payload = await request.json()
        failpoint = None
        if isinstance(payload, dict):
            opts = payload.get("options") or {}
            if isinstance(opts, dict) and opts.get("failpoint_commit"):
                failpoint = "commit"
        return service.merge(payload, failpoint=failpoint)

    @app.get("/v1/runs")
    async def list_runs(limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        return {"runs": service.list_runs(limit=limit)}

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run = service.get_run(run_id)
        if run is None:
            return JSONResponse(status_code=404, content={"error": {"message": "run not found"}})
        return run

    @app.get("/v1/runs/{run_id}/actions")
    async def get_actions(run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "actions": service.get_actions(run_id)}

    @app.get("/v1/runs/{run_id}/traces")
    async def get_traces(run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "traces": service.get_traces(run_id)}

    @app.get("/v1/runs/{run_id}/snapshot")
    async def get_snapshot(run_id: str) -> dict[str, Any]:
        snap = service.get_snapshot(run_id)
        if snap is None:
            return JSONResponse(status_code=404, content={"error": {"message": "snapshot not found"}})
        return {"run_id": run_id, **snap}

    return app


# Query helpers live on MergeService directly (service.py).


def main() -> None:  # pragma: no cover - local server entry point
    import uvicorn

    db_path = os.environ.get("MERGE_DB", "data/merge.db")
    log_path = os.environ.get("MERGE_LOG", "logs/merge-runs.jsonl")
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    service = MergeService(db_path, log_path=log_path)
    app = create_app(service)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("MERGE_PORT", "8080")))


if __name__ == "__main__":  # pragma: no cover
    main()
