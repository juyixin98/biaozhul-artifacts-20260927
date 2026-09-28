"""FastAPI application wiring.

Every request is bound to a ``run_id`` (client may supply one via
``X-Run-Id``) and an input fingerprint, both echoed in logs and responses so
test logs can be correlated to exact inputs.
"""
from __future__ import annotations

import base64
import binascii
import sys

import pyarrow as pa
import fastapi
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.api.schemas import BufferDescriptor, ConcatRequest, IpcImportRequest, SliceRequest
from app.config import Settings, load_settings
from app.core.types import SUPPORTED_TYPES
from app.errors import ErrorCategory, LayoutError
from app.logging_setup import (
    bind_context, configure_logging, fingerprint, get_logger, input_fp_var, run_id_var,
)
from app.service.service import ColumnService
from app.store.metadata import MetadataStore

LOG = get_logger("api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level, settings.log_format)
    LOG.info("starting %s v%s; python/pyarrow=%s/%s",
             settings.app_name, __version__,
             sys.version.split()[0], pa.__version__)

    app = FastAPI(title="Arrow zero-copy views", version=__version__)
    store = MetadataStore(settings.resolved_db_path())
    app.state.settings = settings
    app.state.store = store
    app.state.service = ColumnService(store)

    # ------------------------------------------------------- error handlers
    @app.exception_handler(LayoutError)
    async def layout_error_handler(_request: Request, exc: LayoutError) -> JSONResponse:
        status_code = 404 if exc.category is ErrorCategory.NOT_FOUND else 422
        LOG.warning("categorized failure: %s - %s", exc.category.value, exc.message)
        return JSONResponse(status_code=status_code, content={
            "ok": False,
            "status": "error",
            "error": exc.to_dict(),
        })

    @app.exception_handler(Exception)
    async def unexpected_handler(_request: Request, exc: Exception) -> JSONResponse:
        # Never map unknown states to success; surface them as 500 with the type.
        LOG.exception("unhandled exception")
        return JSONResponse(status_code=500, content={
            "ok": False,
            "status": "error",
            "error": {
                "category": ErrorCategory.INTERNAL_ERROR.value,
                "message": f"{type(exc).__name__}: {exc}",
                "detail": {},
            },
        })

    # ----------------------------------------------------------- middleware
    @app.middleware("http")
    async def bind_run(request: Request, call_next):
        run_id = request.headers.get("x-run-id")
        raw = await request.body()
        body_fp = fingerprint(raw) if raw else "-"

        async def receive() -> dict:
            return {"type": "http.request", "body": raw, "more_body": False}

        # Reconstruct the request so downstream parsers can consume the body
        # that we already read for fingerprinting.
        request = Request(request.scope, receive)
        with bind_context(run_id, body_fp) as ctx:
            LOG.info("%s %s incoming (%d body bytes)",
                     request.method, request.url.path, len(raw))
            response = await call_next(request)
            response.headers["X-Run-Id"] = ctx["run_id"]
            response.headers["X-Input-Fp"] = ctx["input_fp"]
            return response

    # -------------------------------------------------------------- routes
    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "service": settings.app_name,
            "version": __version__,
            "versions": {"python": sys.version.split()[0],
                         "pyarrow": pa.__version__,
                         "fastapi": fastapi.__version__},
            "supported_types": sorted(SUPPORTED_TYPES),
        }

    @app.post("/validate")
    async def validate_endpoint(desc: BufferDescriptor) -> dict:
        return app.state.service.validate_descriptor(
            desc.model_dump(), run_id=run_id_var.get(), input_fp=input_fp_var.get())

    @app.post("/columns")
    async def import_column(desc: BufferDescriptor) -> dict:
        return app.state.service.import_descriptor(
            desc.model_dump(), run_id=run_id_var.get(), input_fp=input_fp_var.get())

    @app.post("/columns/import-ipc")
    async def import_ipc_column(req: IpcImportRequest) -> dict:
        try:
            message = base64.b64decode(req.ipc_stream_b64.encode("ascii"), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise LayoutError(ErrorCategory.MALFORMED_PAYLOAD,
                              f"ipc_stream_b64 is not valid base64: {exc}") from exc
        return app.state.service.import_ipc(
            message, req.column_index, run_id=run_id_var.get(), input_fp=input_fp_var.get())

    @app.get("/columns")
    async def list_columns() -> dict:
        return {"columns": app.state.service.list_columns()}

    @app.get("/columns/{column_id}")
    async def get_column(column_id: str) -> dict:
        return app.state.service.get_column(column_id, run_id=run_id_var.get())

    @app.delete("/columns/{column_id}")
    async def drop_column(column_id: str) -> dict:
        return app.state.service.drop_column(column_id, run_id=run_id_var.get())

    @app.post("/columns/{column_id}/slice")
    async def slice_column(column_id: str, req: SliceRequest) -> dict:
        return app.state.service.slice_column(
            column_id, req.offset, req.length,
            run_id=run_id_var.get(), input_fp=input_fp_var.get())

    @app.post("/columns/concat")
    async def concat_columns(req: ConcatRequest) -> dict:
        return app.state.service.concat_columns(
            req.column_ids, req.target_type,
            run_id=run_id_var.get(), input_fp=input_fp_var.get())

    return app


app = create_app()
