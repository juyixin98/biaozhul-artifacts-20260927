"""HTTP 层：路由与统一错误信封。

端点：
* ``GET  /health``                  存活检查
* ``POST /jobs``                    multipart：file + config(JSON 字符串) + 可选 Idempotency-Key
* ``GET  /jobs``                    列表
* ``GET  /jobs/{job_id}``           查询作业（含区间/失败类别）
* ``GET  /runs/{run_id}``           复现追踪（关键中间状态/事件）
* ``POST /verify``                  验证接口（合成信号 + 守恒/切块不变性核验）
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .errors import ERROR_HTTP_STATUS, SegmentError
from .schemas import ConfigPayload, VerifyRequest
from .service import JobService


def create_app(service: JobService) -> FastAPI:
    app = FastAPI(
        title="Offline PCM dual-threshold silence segmentation",
        version="1.0.0",
    )

    # ---- 统一错误处理：SegmentError / Pydantic / 兜底 ----

    @app.exception_handler(SegmentError)
    async def _segment_error_handler(_: Request, exc: SegmentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(ValidationError)
    async def _validation_handler(_: Request, exc: ValidationError) -> JSONResponse:
        err = SegmentError(
            "INVALID_ARGUMENT",
            "request payload failed validation",
            errors=exc.errors(),
        )
        return JSONResponse(status_code=400, content=err.to_dict())

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:  # noqa: BLE001
        err = SegmentError("INTERNAL", f"unhandled error: {exc!r}")
        return JSONResponse(status_code=500, content=err.to_dict())

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # ---- 作业 ----

    @app.post("/jobs")
    async def create_job(
        file: UploadFile = File(...),
        config: str | None = Form(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        cfg = _parse_config(config, service)
        data = await file.read()
        rec = service.submit(data, cfg, idempotency_key=idempotency_key)
        return rec.to_dict()

    @app.get("/jobs")
    async def list_jobs(limit: int = 100) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        return {"jobs": [r.to_dict() for r in service.store.list_jobs(limit)]}

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        return service.get(job_id).to_dict()

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        return service.trace(run_id)

    @app.post("/verify")
    async def verify(payload: VerifyRequest) -> dict[str, Any]:
        return service.verify(payload.model_dump())

    return app


def _parse_config(raw: str | None, service: JobService) -> dict[str, Any]:
    if raw is None or not raw.strip():
        payload = ConfigPayload()
    else:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SegmentError(
                "INVALID_ARGUMENT",
                "config form field must be a JSON object",
                position=exc.pos,
            ) from exc
        if not isinstance(obj, dict):
            raise SegmentError(
                "INVALID_ARGUMENT", "config must be a JSON object"
            )
        payload = ConfigPayload(**obj)
    return payload.resolved(service.defaults)
