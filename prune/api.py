"""HTTP API: pruning plan, zero-miss validation, catalog inspection.

Endpoints
  GET  /health
  GET  /tables                         catalog summary + pinned versions
  GET  /tables/{name}                  columns, partitions, files
  POST /tables/{name}/prune            -> pruning plan with per-file reasons
  POST /tables/{name}/validate         -> plan + independent full-scan check

All responses carry ``request_id``; failures and uncertain conclusions are
emitted under distinct keys (``failures`` / ``uncertain``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__, values as V
from .catalog import Catalog
from .config import Config, load_config
from .kernel import Justification, Plan, STATS_SCHEMA_VERSION
from .logctx import Trace, new_request_id
from .service import RequestError, build_context, run_plan
from .models import parse_predicate
from .parquet_adapter import ADAPTER_VERSION
from .reference import validate_plan_zero_miss
from .transforms import TRANSFORM_SPEC_VERSION, tzdb_version


def _jdump(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=V.json_default))


def _just(j: Justification) -> dict:
    return {"code": j.code, "detail": j.detail, "leaf": j.leaf}


def plan_to_dict(plan: Plan) -> dict:
    return {
        "table": plan.table,
        "request_id": plan.request_id,
        "versions": {
            "transform_spec": plan.transform_version,
            "tzdb": plan.tzdb_version,
            "stats_schema": plan.stats_schema_version,
        },
        "candidate_partitions": plan.candidates,
        "partitions": [
            {"label": p.label, "is_null": p.is_null,
             "verdict": p.verdict,
             "reason": _just(p.reason) if p.reason else None,
             "files": [{"path": f.path, "verdict": f.verdict,
                        "num_rows": f.num_rows, "size_bytes": f.size_bytes,
                        "reasons": [_just(r) for r in f.reasons]}
                       for f in p.files]}
            for p in plan.partitions
        ],
        "metrics": plan.metrics,
        "uncertain": plan.uncertain,
        "failures": plan.failures,
    }


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="two-level-prune", version=__version__)
    app.state.config = config

    @app.middleware("http")
    async def bind_trace(request: Request, call_next):
        rid = request.headers.get("x-request-id") or new_request_id()
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    def catalog(self=None) -> Catalog:
        return Catalog(app.state.config.catalog_db)

    @app.exception_handler(RequestError)
    async def request_error_handler(request: Request, exc: RequestError):
        rid = getattr(request.state, "request_id", new_request_id())
        return JSONResponse(status_code=exc.status, content={
            "request_id": rid,
            "error": {"code": exc.code, "message": str(exc)},
        })

    @app.get("/health")
    def health(request: Request):
        return {"status": "ok", "service": __version__,
                "request_id": request.state.request_id,
                "versions": {"transform_spec": TRANSFORM_SPEC_VERSION,
                             "tzdb": tzdb_version(),
                             "stats_schema": STATS_SCHEMA_VERSION,
                             "adapter": ADAPTER_VERSION}}

    @app.get("/tables")
    def tables(request: Request):
        with catalog() as cat:
            names = cat.table_names()
        return {"request_id": request.state.request_id,
                "configured": sorted(app.state.config.tables),
                "refreshed": names}

    @app.get("/tables/{name}")
    def table_detail(name: str, request: Request):
        cfg = app.state.config
        if name not in cfg.tables:
            raise RequestError("UNKNOWN_TABLE", f"table {name!r} not configured", 404)
        with catalog() as cat:
            ctx = cat.load_context(cfg.tables[name], name)
        return {
            "request_id": request.state.request_id,
            "table": name,
            "columns": [{"name": c.name, "type": c.type} for c in ctx.columns.values()],
            "transform": (None if ctx.transform is None else {
                "kind": ctx.transform.kind,
                "source_column": ctx.transform.source_column,
                "tz": ctx.transform.tz_name,
                "spec_version": ctx.recorded_transform_version,
                "tzdb_version": ctx.recorded_tzdb_version,
            }),
            "partitions": [
                {"label": p.label, "is_null": p.is_null,
                 "files": [{"path": f.path, "num_rows": f.num_rows,
                            "size_bytes": f.size_bytes,
                            "row_groups": f.row_groups,
                            "stats_version": f.stats_version,
                            "pyarrow_version": f.pyarrow_version}
                           for f in p.files]}
                for p in ctx.partitions],
        }

    @app.post("/tables/{name}/prune")
    async def prune(name: str, request: Request):
        body = await request.json()
        trace = Trace(request.state.request_id, table=name)
        with catalog() as cat:
            plan = run_plan(app.state.config, cat, name, body, trace)
        return JSONResponse(content=_jdump({
            "request_id": request.state.request_id,
            **plan_to_dict(plan),
            "trace": trace.public(),
        }))

    @app.post("/tables/{name}/validate")
    async def validate(name: str, request: Request):
        body = await request.json()
        id_column = body.pop("id_column", "id")
        predicate = body.get("predicate", body)
        trace = Trace(request.state.request_id, table=name)
        with catalog() as cat:
            # Run the plan first so the trace and metrics are reported even if
            # the reference scan fails independently.
            plan = run_plan(app.state.config, cat, name, predicate, trace)
            spec, ctx = build_context(app.state.config, cat, name)
            if id_column not in ctx.columns:
                raise RequestError("UNKNOWN_ID_COLUMN",
                                   f"id column {id_column!r} not in schema", 400)
            domains = {c: col.type for c, col in ctx.columns.items()}
            all_paths, kept, pruned = [], [], []
            for p in plan.partitions:
                for f in p.files:
                    all_paths.append(f.path)
                    (kept if f.verdict != "PRUNED" else pruned).append(f.path)
            trace.step("reference_scan", "full PyArrow scan of every file",
                       location="reference", files=len(all_paths))
            verdict = validate_plan_zero_miss(
                all_paths=all_paths, kept_paths=kept, pruned_paths=pruned,
                predicate=parse_predicate(predicate),
                id_column=id_column, domains=domains)
        if verdict["ok"]:
            trace.step("zero_miss", "no matching row lives in a pruned file",
                       location="reference", expected=verdict["expected_matching_rows"])
        else:
            trace.failure(verdict["failure_category"],
                          verdict.get("detail", "matching rows were pruned"),
                          location="reference")
        return JSONResponse(content=_jdump({
            "request_id": request.state.request_id,
            **plan_to_dict(plan),
            "validation": verdict,
            "trace": trace.public(),
        }))

    return app


def app_from_config(path: str | Path) -> FastAPI:
    return create_app(load_config(path))
