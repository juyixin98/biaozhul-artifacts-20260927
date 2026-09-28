"""FastAPI application wiring config, service, registry and metadata store."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from arrowzero import __version__
from arrowzero.api.schemas import (
    ConcatRequest,
    HandleRequest,
    ImportRequest,
    SliceRequest,
    ValidateRequest,
)
from arrowzero.config import get_settings
from arrowzero.metadata import MetadataStore
from arrowzero.observability import RunLogger
from arrowzero.service import OperationResult, Registry, ViewService
from arrowzero.versions import runtime_versions

_REJECTED_STATUS = 422
_FAILED_STATUS = 500
_NOT_FOUND_STATUS = 404
_CATEGORY_STATUS = {
    "NOT_FOUND": _NOT_FOUND_STATUS,
    "FORMAT": _REJECTED_STATUS,
    "VALIDATION": _REJECTED_STATUS,
    "TYPE_MISMATCH": _REJECTED_STATUS,
    "SLICE_RANGE": _REJECTED_STATUS,
    "BAD_REQUEST": _REJECTED_STATUS,
}


def create_app(settings=None) -> FastAPI:
    settings = settings or get_settings()
    logger = RunLogger(settings.log_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.store = MetadataStore(settings.db_path)
        app.state.registry = Registry(settings.registry_capacity)
        app.state.service = ViewService(
            app.state.store, app.state.registry, logger, run_purpose="api"
        )
        logger.emit(
            "startup", "started", step="startup", verdict="committed",
            detail={"settings": str(settings.__dict__), "versions": runtime_versions()},
        )
        try:
            yield
        finally:
            app.state.store.close()

    app = FastAPI(
        title="arrowzero",
        version=__version__,
        description="Safe zero-copy Arrow column views: import, slice, concat.",
        lifespan=lifespan,
    )

    def service(request: Request) -> ViewService:
        return request.app.state.service

    def respond(result: OperationResult):
        if result.status == "committed":
            return {"run_id": result.run_id, "op_id": result.op_id, "result": result.payload}
        assert result.error is not None
        code = result.error.get("code", "FAILED")
        http_status = 500 if result.status == "failed" else _CATEGORY_STATUS.get(code, 400)
        return JSONResponse(
            status_code=http_status,
            content={
                "run_id": result.run_id,
                "op_id": result.op_id,
                "status": result.status,
                "error": result.error,
            },
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "versions": runtime_versions()}

    def run_id_for(req_body, request: Request) -> str | None:
        # Header wins (set by the client once); body field remains for callers
        # that do not control headers.
        return request.headers.get("x-run-id") or getattr(req_body, "run_id", None)

    @app.post("/api/v1/arrays/import")
    def import_array(req: ImportRequest, request: Request):
        run_id = run_id_for(req, request)
        return respond(service(request).import_array(req.to_service_dict(), run_id=run_id))

    @app.post("/api/v1/arrays/slice")
    def slice_array(req: SliceRequest, request: Request):
        return respond(
            service(request).slice_array(
                req.handle, req.offset, req.length, run_id=run_id_for(req, request)
            )
        )

    @app.post("/api/v1/arrays/concat")
    def concat_arrays(req: ConcatRequest, request: Request):
        return respond(
            service(request).concat_arrays(
                req.handles, req.cast_to, run_id=run_id_for(req, request)
            )
        )

    @app.post("/api/v1/arrays/values")
    def get_values(req: HandleRequest, request: Request):
        return respond(
            service(request).get_values(req.handle, run_id=run_id_for(req, request))
        )

    @app.post("/api/v1/arrays/export")
    def export_array(req: HandleRequest, request: Request):
        return respond(
            service(request).export_array(req.handle, run_id=run_id_for(req, request))
        )

    @app.post("/api/v1/validate")
    def validate(req: ValidateRequest, request: Request):
        return respond(
            service(request).validate_descriptor(
                req.to_descriptor(), run_id=request.headers.get("x-run-id")
            )
        )

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str, request: Request):
        run = request.app.state.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"unknown run_id {run_id}")
        ops = request.app.state.store.list_operations(run_id=run_id, limit=200)
        arrays = request.app.state.store.list_arrays(run_id=run_id)
        return {"run": run, "operations": ops, "arrays": arrays}

    return app


app = create_app()
