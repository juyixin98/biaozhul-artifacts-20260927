"""FastAPI application and routes."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .. import __version__
from ..adapters import arrowio
from ..config import Settings
from ..core.errors import DicunifyError
from ..service.engine import UnifyEngine
from ..service.identity import component_versions, new_run_id
from ..service.logging_setup import configure_logging
from ..store.sqlite_store import JobStore
from .schemas import UnifyRequest, VerifyRequest


def create_app(settings: Settings | None = None,
               store: JobStore | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    log = configure_logging(settings.log_level)
    if settings.db_path != Path(":memory:"):
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    own_store = store is None
    store = store or JobStore(settings.db_path)
    engine = UnifyEngine(settings, store)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if own_store:
            store.close()

    app = FastAPI(
        title="Multi-batch Dictionary Unification Service",
        version=__version__,
        description="统一多个列批次的字典编码，输出全局字典与每批重映射。",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store

    @app.exception_handler(DicunifyError)
    async def _domain_error_handler(_: Request, exc: DicunifyError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error": {"code": exc.code, "message": exc.message,
                          "details": exc.details},
            },
        )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "dicunify", "version": __version__,
                "versions": component_versions()}

    @app.post("/api/v1/unify")
    async def unify_json(req: UnifyRequest) -> dict:
        # Pydantic already validated shapes; pass through as plain dict so the
        # adapter owns domain coercion and classified errors.
        payload = req.model_dump()
        return engine.run_json(payload).body

    @app.post("/api/v1/unify/arrow")
    async def unify_arrow(
        request: Request,
        value_type: str,
        index_policy: str = "auto",
        target_width: int | None = None,
        client_run_id: str | None = None,
    ) -> Response:
        body = await request.body()
        ipc, _job_id = engine.run_arrow(
            body, value_type=value_type, index_policy=index_policy,
            target_width=target_width, client_run_id=client_run_id,
        )
        return Response(content=ipc, media_type="application/vnd.apache.arrow.stream")

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        job = store.get_job(job_id)
        if job is None:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "JOB_NOT_FOUND",
                                   "message": f"no job {job_id!r}"}},
            )
        return job

    @app.post("/api/v1/verify")
    async def verify_endpoint(req: VerifyRequest, request: Request) -> dict:
        run_id = new_run_id()
        job_id = f"verify-{run_id}"
        log.info("stateless verify called",
                 extra={"run_id": run_id, "job_id": job_id, "step": "verify"})
        return engine.verify_job_decoding(job_id, req.model_dump())

    return app


app = create_app()
