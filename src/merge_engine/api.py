"""FastAPI 验证接口层。

路由：
  POST /v1/merge/dry-run    只决策、不写库（返回完整动作集合供人工/程序核验）
  POST /v1/merge            决策 + 原子提交
  GET  /v1/runs/{run_id}    运行元数据（状态、错误类别）
  GET  /v1/runs/{run_id}/actions  已提交运行的动作审计
  GET  /v1/targets/{table}/rows   目标表现行内容（核验无部分更新）
  GET  /healthz

错误响应统一为 MergeError.to_dict()，HTTP 状态按四类映射。
fault_point 是本地合成测试设施，只经 Python API/测试使用；HTTP 层默认不暴露。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .engine import MergeEngine, MergeRequest
from .errors import CATEGORY_HTTP, ErrorCategory, MergeError


# ---- 请求模型（显式联合，禁止额外字段） --------------------------------------

class RecordsSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["records"]
    records: list[dict[str, Any]]


class NdjsonSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["ndjson"]
    content: str | None = None
    path: str | None = None


class ParquetSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["parquet"]
    path: str


class MergeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: dict[str, Any]
    config: dict[str, Any] = Field(..., description="MergeSpec 原始配置")
    dry_run: bool = False
    fault_point: Literal["after_actions", "before_commit", "commit_raises"] | None = None


# ---- 应用工厂 ---------------------------------------------------------------

def create_app(db_path: str | Path, journal_dir: str | Path,
               *, allow_fault_injection: bool = False) -> FastAPI:
    engine = MergeEngine(db_path, journal_dir)
    app = FastAPI(
        title="Composite-key MERGE decision engine",
        version="0.1.0",
        openapi_tags=[{"name": "v1"}],
    )

    def get_engine() -> MergeEngine:
        return engine

    def _execute(body: MergeBody, dry_run: bool) -> dict[str, Any]:
        if body.fault_point is not None and not allow_fault_injection:
            raise HTTPException(
                status_code=400,
                detail={
                    "category": ErrorCategory.INPUT_ERROR.value,
                    "code": "FAULT_INJECTION_DISABLED",
                    "message": "fault injection is disabled on this server",
                    "details": {},
                },
            )
        request = MergeRequest(
            source=body.source,
            config=body.config,
            dry_run=dry_run,
            fault_point=body.fault_point,
        )
        try:
            result = engine.run(request)
        except MergeError as exc:
            # engine 已把可预期错误转成 RunResult；漏网的同构错误仍按契约返回
            raise HTTPException(status_code=exc.http_status, detail=exc.to_dict())
        payload = result.to_dict()
        if result.status in ("COMMITTED", "PLANNED"):
            status = 200
        else:
            # REJECTED / FAILED：按错误契约的四类类别映射 HTTP 状态
            category = ErrorCategory(result.error["category"])
            status = CATEGORY_HTTP[category]
        return JSONResponse(status_code=status, content=payload)

    @app.post("/v1/merge/dry-run", tags=["v1"])
    def dry_run(body: MergeBody, eng: MergeEngine = Depends(get_engine)) -> dict[str, Any]:
        body.dry_run = True
        return _execute(body, dry_run=True)

    @app.post("/v1/merge", tags=["v1"])
    def merge(body: MergeBody, eng: MergeEngine = Depends(get_engine)) -> dict[str, Any]:
        return _execute(body, dry_run=bool(body.dry_run))

    @app.get("/v1/runs/{run_id}", tags=["v1"])
    def get_run(run_id: str, eng: MergeEngine = Depends(get_engine)) -> dict[str, Any]:
        run = eng.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail={"run_id": run_id,
                                                         "message": "run not found"})
        return run

    @app.get("/v1/runs/{run_id}/actions", tags=["v1"])
    def get_actions(run_id: str, eng: MergeEngine = Depends(get_engine)) -> dict[str, Any]:
        if eng.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail={"run_id": run_id,
                                                         "message": "run not found"})
        return {"run_id": run_id, "actions": eng.get_actions(run_id)}

    @app.get("/v1/targets/{table}/rows", tags=["v1"])
    def get_target_rows(table: str, eng: MergeEngine = Depends(get_engine)) -> dict[str, Any]:
        return {"table": table, "rows": eng.get_target_rows(table)}

    @app.get("/healthz", tags=["v1"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
