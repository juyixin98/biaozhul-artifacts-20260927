"""FastAPI 应用：健康检查、场景、原始报文仿真（同步/异步作业）、作业查询。

错误语义（HTTP 状态 + 稳定 error_code）：

- 400 bad_request / bad_packet / bad_encoding / missing_field
- 404 not_found（作业或场景不存在）
- 422 由 Pydantic 请求体校验自动产生
- 500 internal_error（不确定/失败在结果体里单列，不伪装成功）
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.api.schemas import RawSimRequest, ScenarioRequest
from app.core import pipeline
from app.core.scenarios import SCENARIOS, run_all
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.logging_setup import configure_logging, get_logger, set_request_id

log = get_logger("app.api")


def create_app(db_path: str = "data/jitter.db",
               log_level: str = "INFO",
               log_json: bool = True) -> FastAPI:
    configure_logging(log_level, log_json)
    app = FastAPI(
        title="离线 RTP 抖动缓冲与播放计划后端",
        version=__version__,
        description="本地合成夹具 + 自适应/固定双臂仿真 + 独立 oracle 复核")
    store = JobStore(db_path)
    runner = JobRunner(store)

    def handle_scenario(payload: dict[str, Any]) -> dict[str, Any]:
        return pipeline.run_scenario_named(
            payload["scenario"], payload.get("fixed_delay_us"))

    def handle_raw(payload: dict[str, Any]) -> dict[str, Any]:
        req = RawSimRequest(**payload)
        return pipeline.run_raw_packets(
            [p.model_dump() for p in req.packets],
            req.params.model_dump(exclude_none=True) if req.params else None,
            req.expected_clock_ratio,
            fixed_delay_us=req.params.fixed_delay_us
            if req.params and req.params.fixed_delay_us else 40_000)

    runner.register("scenario", handle_scenario)
    runner.register("raw", handle_raw)
    runner.start()
    app.state.store = store
    app.state.runner = runner

    # ---- 请求身份中间件 --------------------------------------------------
    @app.middleware("http")
    async def correlation(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        set_request_id(rid)
        log.info("http_request", extra={"fields": {
            "method": request.method, "path": request.url.path}})
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Service-Version"] = __version__
        return response

    def _err(status: int, code: str, message: str, request: Request,
             details: Any = None) -> JSONResponse:
        rid = request.headers.get("X-Request-ID", "-")
        return JSONResponse(status_code=status, content={
            "error_code": code, "message": message,
            "request_id": rid, "details": details})

    # ---- 健康/版本 -------------------------------------------------------
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "module": "app.api:create_app",
            "scenarios": sorted(SCENARIOS),
        }

    # ---- 场景：同步 ------------------------------------------------------
    @app.post("/validate/scenario")
    def validate_scenario(body: ScenarioRequest, request: Request) -> JSONResponse:
        if body.scenario not in SCENARIOS:
            return _err(404, "unknown_scenario",
                        f"未知场景 {body.scenario!r}", request,
                        {"available": sorted(SCENARIOS)})
        try:
            result = pipeline.run_scenario_named(
                body.scenario, body.fixed_delay_us)
        except pipeline.PipelineError as exc:
            return _err(400, exc.code, exc.detail, request)
        return JSONResponse(result)

    @app.get("/scenarios")
    def list_scenarios() -> dict[str, Any]:
        return {"scenarios": [
            {"name": n, "description": fn.__doc__.strip().splitlines()[0]}
            for n, fn in SCENARIOS.items()]}

    @app.post("/validate/all")
    def validate_all() -> dict[str, Any]:
        reports = run_all()
        return {
            "results": [r.to_dict() for r in reports],
            "all_passed": all(r.passed for r in reports),
            "failed": [r.scenario for r in reports if not r.passed],
        }

    # ---- 原始报文：同步 --------------------------------------------------
    @app.post("/validate/plan")
    def validate_plan(body: RawSimRequest, request: Request) -> JSONResponse:
        try:
            result = pipeline.run_raw_packets(
                [p.model_dump() for p in body.packets],
                body.params.model_dump(exclude_none=True) if body.params else None,
                body.expected_clock_ratio,
                fixed_delay_us=body.params.fixed_delay_us
                if body.params and body.params.fixed_delay_us else 40_000)
        except pipeline.PipelineError as exc:
            return _err(400, exc.code, exc.detail, request)
        return JSONResponse(result)

    # ---- 异步作业 --------------------------------------------------------
    @app.post("/jobs/scenario", status_code=202)
    def submit_scenario_job(body: ScenarioRequest, request: Request):
        if body.scenario not in SCENARIOS:
            return _err(404, "unknown_scenario",
                        f"未知场景 {body.scenario!r}", request,
                        {"available": sorted(SCENARIOS)})
        rid = request.headers.get("X-Request-ID", "-")
        job_id = runner.submit("scenario", rid, body.model_dump())
        return JSONResponse(
            status_code=202,
            content={"job_id": job_id, "status": "queued",
                     "location": f"/jobs/{job_id}"})

    @app.post("/jobs/raw", status_code=202)
    def submit_raw_job(body: RawSimRequest, request: Request):
        rid = request.headers.get("X-Request-ID", "-")
        payload = body.model_dump()
        # 入站即校验报文编码，避免坏作业静默排队后才失败
        try:
            pipeline._decode_packets(payload["packets"])
        except pipeline.PipelineError as exc:
            return _err(400, exc.code, exc.detail, request)
        job_id = runner.submit("raw", rid, payload)
        return JSONResponse(
            status_code=202,
            content={"job_id": job_id, "status": "queued",
                     "location": f"/jobs/{job_id}"})

    @app.get("/jobs")
    def list_jobs(limit: int = 50) -> dict[str, Any]:
        return {"jobs": store.list_jobs(limit=min(limit, 200))}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        row = store.get(job_id)
        if row is None:
            return _err(404, "not_found", f"作业 {job_id} 不存在", request)
        return row

    # ---- 全局错误处理 ----------------------------------------------------
    @app.exception_handler(pipeline.PipelineError)
    async def pipeline_error_handler(request: Request, exc: pipeline.PipelineError):
        return _err(400, exc.code, exc.detail, request)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_exception", exc_info=True)
        return _err(500, "internal_error", str(exc), request)

    return app
