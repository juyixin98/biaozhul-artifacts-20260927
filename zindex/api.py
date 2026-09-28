"""FastAPI validation interface.

Every response shares one envelope so failures and uncertain conclusions are
first-class fields rather than text in a message:

    {
      "request_id": "...", "engine_version": "...", "status": "complete|degraded|error",
      "schema": ..., "data": ..., "steps": [...], "stats": {...},
      "errors": [ {"category": "...", "message": "..."} ],
      "uncertainties": [ ... ]
    }

A middleware assigns (or honors an inbound ``X-Request-ID``) request id,
propagates it into structured logs, and persists a row in ``request_log`` so
``GET /requests/{id}`` can reconstruct who/when/where/how for any call.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import ENGINE_VERSION, __version__
from .catalog import Catalog
from .config import Settings
from .errors import ZIndexError
from .ingest import IngestService
from .kernel import Kernel
from .logging_setup import configure_logging, get_logger, set_request_id


class DimIn(BaseModel):
    name: str
    bits: int = Field(ge=1, le=64)
    signed: bool = True


class SchemaIn(BaseModel):
    name: str = "default"
    dims: list[DimIn]


class IngestIn(BaseModel):
    rows: list[list[int]]
    capacity: int | None = Field(default=None, ge=1)


class SyntheticIn(BaseModel):
    n: int = Field(ge=1, le=5_000_000)
    shape: str = "uniform"
    seed: int = 1
    capacity: int | None = Field(default=None, ge=1)


class QueryIn(BaseModel):
    lo: list[int]
    hi: list[int]
    max_intervals: int | None = Field(default=None, ge=1)
    limit: int | None = Field(default=None, ge=0)


class RewriteIn(BaseModel):
    capacity: int | None = Field(default=None, ge=1)


def create_app(settings: Settings) -> FastAPI:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(settings.log_file, settings.log_level)
    log = get_logger("api")
    catalog = Catalog(settings.catalog_path)
    ingest = IngestService(catalog, settings.data_dir, settings.default_chunk_capacity)
    kernel = Kernel(catalog, settings.data_dir)

    app = FastAPI(
        title="Morton clustered columnar range-query backend",
        version=__version__,
    )
    app.state.settings = settings
    app.state.catalog = catalog

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:16]}"
        set_request_id(rid)
        request.state.request_id = rid
        log.info("request_started", extra={"data": {"method": request.method, "path": request.url.path}})
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        log.info("request_finished", extra={"data": {"method": request.method, "path": request.url.path, "status": response.status_code}})
        return response

    def envelope(
        request: Request,
        *,
        status: str,
        schema: str | None = None,
        data: Any = None,
        steps: list[dict] | None = None,
        stats: dict | None = None,
        errors: list[dict] | None = None,
        uncertainties: list[dict] | None = None,
        http_status: int = 200,
        raw_payload: Any = None,
        kind: str = "query",
    ) -> JSONResponse:
        rid = request.state.request_id
        body = {
            "request_id": rid,
            "engine_version": ENGINE_VERSION,
            "package_version": __version__,
            "status": status,
            "schema": schema,
            "data": data,
            "steps": steps or [],
            "stats": stats or {},
            "errors": errors or [],
            "uncertainties": uncertainties or [],
        }
        try:
            catalog.log_request({
                "request_id": rid,
                "kind": kind,
                "schema": schema,
                "payload": json.dumps(raw_payload, default=str)[:4000] if raw_payload is not None else None,
                "status": status,
                "http_status": http_status,
                "num_results": (stats or {}).get("returned_rows"),
                "candidates": (stats or {}).get("code_candidates"),
                "chunks_read": (stats or {}).get("chunks_selected"),
                "io_bytes": (stats or {}).get("chunk_bytes_read"),
                "uncertainty": json.dumps(uncertainties, default=str) if uncertainties else None,
            })
        except Exception:  # logging must never break a response
            log.exception("request_log_failed")
        return JSONResponse(body, status_code=http_status)

    # ------------------------------------------------------------- meta / schema
    @app.get("/health")
    async def health(request: Request):
        return envelope(request, status="complete", data={
            "status": "ok",
            "engine": ENGINE_VERSION,
            "version": __version__,
            "schemas": catalog.list_schemas(),
        }, kind="health")

    @app.post("/schemas", status_code=201)
    async def create_schema(payload: SchemaIn, request: Request, overwrite: bool = False):
        spec = ingest.create_schema(
            payload.name, [d.model_dump() for d in payload.dims], overwrite=overwrite
        )
        return envelope(request, status="complete", schema=payload.name,
                        data=spec.to_dict(), kind="schema_create",
                        raw_payload=payload.model_dump(), http_status=201)

    @app.put("/schemas/{name}")
    async def replace_schema(name: str, payload: SchemaIn, request: Request):
        payload.name = name
        spec = ingest.replace_schema(name, [d.model_dump() for d in payload.dims])
        return envelope(request, status="complete", schema=name, data=spec.to_dict(),
                        kind="schema_replace", raw_payload=payload.model_dump())

    @app.get("/schemas")
    async def list_schemas(request: Request):
        data = [catalog.get_schema(n).to_dict() for n in catalog.list_schemas()]
        return envelope(request, status="complete", data=data, kind="schema_list")

    @app.get("/schemas/{name}")
    async def get_schema(name: str, request: Request):
        spec = catalog.get_schema(name)
        chunks = catalog.list_chunks(name)
        return envelope(request, status="complete", schema=name, data={
            "spec": spec.to_dict(),
            "chunks": [{
                "chunk_id": c.chunk_id,
                "num_rows": c.num_rows,
                "min_code": str(c.min_code),
                "max_code": str(c.max_code),
                "byte_size": c.byte_size,
                "path": c.path,
            } for c in chunks],
        }, kind="schema_get")

    # ------------------------------------------------------------------ ingest
    @app.post("/schemas/{name}/ingest", status_code=201)
    async def do_ingest(name: str, payload: IngestIn, request: Request):
        result = ingest.ingest_rows(name, payload.rows, capacity=payload.capacity)
        return envelope(request, status="complete", schema=name,
                        data={"rows_ingested": result.rows_ingested,
                              "chunks": result.chunk_summaries},
                        kind="ingest", raw_payload={"rows": len(payload.rows)},
                        http_status=201)

    @app.post("/schemas/{name}/ingest_synthetic", status_code=201)
    async def do_synthetic(name: str, payload: SyntheticIn, request: Request):
        result = ingest.ingest_synthetic(
            name, payload.n, shape=payload.shape, seed=payload.seed, capacity=payload.capacity
        )
        return envelope(request, status="complete", schema=name,
                        data={"rows_ingested": result.rows_ingested,
                              "shape": payload.shape, "seed": payload.seed,
                              "chunks": result.chunk_summaries},
                        kind="ingest_synthetic", raw_payload=payload.model_dump(),
                        http_status=201)

    # ------------------------------------------------------------------- query
    @app.post("/schemas/{name}/query")
    async def do_query(name: str, payload: QueryIn, request: Request):
        budget = payload.max_intervals or settings.default_max_intervals
        outcome = kernel.query(name, payload.lo, payload.hi, budget, limit=payload.limit)
        return envelope(
            request,
            status=outcome.status,
            schema=name,
            data={"rows": outcome.rows},
            steps=outcome.steps,
            stats=outcome.stats,
            uncertainties=outcome.uncertainties,
            raw_payload=payload.model_dump(),
            kind="query",
        )

    # ----------------------------------------------------------------- rewrite
    @app.post("/schemas/{name}/rewrite")
    async def do_rewrite(name: str, payload: RewriteIn, request: Request):
        result = ingest.rewrite_all(name, capacity=payload.capacity)
        return envelope(request, status="complete", schema=name, data=result,
                        kind="rewrite", raw_payload=payload.model_dump())

    # ----------------------------------------------------------- request audit
    @app.get("/requests/{request_id}")
    async def get_request(request_id: str, request: Request):
        row = catalog.get_request(request_id)
        if row is None:
            raise SchemaNotFound(f"request {request_id!r} not found", request_id=request_id)
        return envelope(request, status="complete", data=row, kind="request_get")

    @app.get("/requests")
    async def list_requests(request: Request, limit: int = 50):
        return envelope(request, status="complete",
                        data=catalog.list_requests(min(limit, 500)), kind="request_list")

    # ------------------------------------------------------------- error maps
    @app.exception_handler(ZIndexError)
    async def zindex_error_handler(request: Request, exc: ZIndexError):
        log.warning("request_failed", extra={"data": {"category": exc.category, "details": exc.details}})
        return envelope(
            request, status="error",
            schema=request.path_params.get("name"),
            errors=[exc.to_dict()],
            http_status=exc.http_status,
            kind=request.url.path.rstrip("/").split("/")[-1],
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        log.exception("unhandled_exception")
        return envelope(
            request, status="error",
            schema=request.path_params.get("name"),
            errors=[{"category": "internal_error", "message": str(exc)}],
            http_status=500,
            kind="unhandled",
        )

    return app


def create_default_app() -> FastAPI:
    return create_app(Settings.load(Path("configs/dev.json")))


app = create_default_app()
