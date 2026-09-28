"""FastAPI application: validation surface over the execution core.

Request identity: every response (including errors) carries ``request_id``;
the id is either client-supplied (``X-Request-Id``) or generated, and it is the
same key under which structured log lines and audit rows are stored.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import Config
from ..core.store import Store
from ..errors import ZClusterError
from ..format.chunks import CHUNK_FORMAT_VERSION
from ..kernel.coder import FORMAT_VERSION
from ..logging_setup import configure_logging, get_logger, set_request_id
from ..verification.checks import CHECKS_VERSION, run_all
from .schemas import IngestIn, QueryIn, SchemaIn

log = get_logger("api")



def create_app(config: Config) -> FastAPI:
    configure_logging(config.log_level, config.log_file)
    store = Store(config)

    app = FastAPI(
        title="zcluster — Morton-clustered integer column store",
        version=__version__,
        docs_url="/docs",
    )
    app.state.config = config
    app.state.store = store

    @app.middleware("http")
    async def bind_request_id(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or f"req-{uuid.uuid4().hex[:16]}"
        set_request_id(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error", extra={"step": "http", "uncertain": True})
            raise
        response.headers["X-Request-Id"] = request_id
        return response

    @app.exception_handler(ZClusterError)
    async def zcluster_error_handler(request: Request, exc: ZClusterError):
        rid = getattr(request.state, "request_id", "-")
        log.warning("request failed", extra={"step": "error",
                                             "detail": {"category": exc.category}})
        return JSONResponse(
            status_code=exc.http_status,
            content={"request_id": rid, "error_category": exc.category,
                     "message": str(exc), "detail": None, "uncertainties": []},
        )

    @app.get("/health")
    async def health():
        ds = store.catalog.dataset_row()
        return {"status": "ok", "service": "zcluster", "version": __version__,
                "request_id_mechanism": "X-Request-Id header or server-generated",
                "dataset_initialized": ds is not None,
                "coder_version": FORMAT_VERSION,
                "chunk_format_version": CHUNK_FORMAT_VERSION,
                "checks_version": CHECKS_VERSION,
                "config": {"data_root": config.data_root,
                           "chunk_size": config.chunk_size,
                           "config_file": config.source_path}}

    @app.post("/api/schema")
    async def create_schema(payload: SchemaIn, request: Request):
        rid = request.state.request_id
        info = store.initialize(payload.name, [d.model_dump() for d in payload.dimensions], rid)
        return {"request_id": rid, "schema": info}

    @app.get("/api/schema")
    async def get_schema(request: Request):
        return {"request_id": request.state.request_id, "schema": store.schema_info()}

    @app.post("/api/ingest")
    async def ingest(payload: IngestIn, request: Request):
        rid = request.state.request_id
        result = store.ingest(payload.rows, rid)
        return {"request_id": rid, "ingest": result}

    @app.post("/api/query")
    async def query(payload: QueryIn, request: Request):
        rid = request.state.request_id
        outcome = store.query([e.model_dump() for e in payload.box],
                              rid, budget=payload.interval_budget)
        return {
            "request_id": rid,
            "box": outcome.box,
            "rows": outcome.rows,
            "intervals": outcome.intervals,
            "stats": outcome.stats,
            "steps": outcome.steps,
            "uncertainties": outcome.uncertainties,
            "budget_exhausted": outcome.budget_exhausted,
        }

    @app.post("/api/full-scan")
    async def full_scan(payload: QueryIn, request: Request):
        rid = request.state.request_id
        result = store.full_scan([e.model_dump() for e in payload.box], rid)
        return {"request_id": rid, **result}

    @app.post("/api/compact")
    async def compact(request: Request):
        rid = request.state.request_id
        return {"request_id": rid, "compact": store.compact(rid)}

    @app.get("/api/chunks")
    async def chunks(request: Request):
        return {"request_id": request.state.request_id,
                "chunks": store.chunk_descriptors()}

    @app.get("/api/requests/{request_id}")
    async def get_request(request_id: str):
        return store.catalog.get_audit(request_id)

    @app.get("/api/requests")
    async def list_requests(limit: int = 50):
        return {"requests": store.catalog.list_audit(limit)}

    @app.post("/api/verify")
    async def verify(request: Request):
        """Run the built-in proof suite on throwaway local fixtures.

        Never touches the live dataset: each check builds its own store under a
        temporary data root.
        """
        rid = request.state.request_id
        report = run_all(config, temporary_store_factory(config))
        store.catalog.record_audit(
            rid, "verify", "ok" if report["ok"] else "failed",
            f"{sum(1 for c in report['checks'] if c['ok'])}/{len(report['checks'])} checks passed",
            {"failure_categories": report["failure_categories"]},
        )
        log.info("verification complete",
                 extra={"step": "verify", "version": CHECKS_VERSION,
                        "detail": {"ok": report["ok"],
                                   "failures": report["failure_categories"]}})
        return {"request_id": rid, "report": report}

    return app


def temporary_store_factory(config: Config):
    """Callable compatible with checks.make_store: yields an isolated Store.

    Roots live in the system temp dir, never under the live ``data_root`` —
    a root nested inside the live root would make the Store open the live
    catalog while walking up to find an existing dataset directory.
    """
    from contextlib import contextmanager as _cm
    import dataclasses
    import tempfile

    @_cm
    def factory(cfg):
        tmp = tempfile.mkdtemp(prefix="zcluster-verify-")
        isolated = dataclasses.replace(cfg, data_root=tmp)
        s = Store(isolated)
        try:
            yield s
        finally:
            s.close()
    return factory


def create_default_app() -> FastAPI:
    return create_app(Config.load())


# Instantiated for ``uvicorn zcluster.api.app:app``.  Skipped under pytest so
# importing the module to build isolated test apps never touches the real
# data root; tests always go through create_app(test_config).
import sys as _sys  # noqa: E402

if "pytest" not in _sys.modules:
    app = create_default_app()
