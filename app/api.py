"""FastAPI 审计接口与编排。

错误契约：所有领域错误统一返回
    {"error": {"category", "code", "message", "details"}}
category ∈ input_error | state_conflict | resource_exhausted | computation_failure。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .errors import AuditError, ErrorCode
from .kernel import analyze as kernel_analyze
from .kernel import remediate as kernel_remediate
from .models import (
    AnalysisView,
    CreateRunRequest,
    EvidenceBatch,
    EventView,
    Policy,
    RemediationView,
    RunView,
    VerifyView,
)
from .storage import AuditStorage

DEFAULT_DB = os.environ.get("AUDIT_DB", str(Path("data") / "audit.sqlite3"))
DEFAULT_MASTER_KEY = os.environ.get(
    "AUDIT_MASTER_KEY",
    # 仅用于本地演示的开发主密钥；生产部署必须通过环境变量注入
    "local-dev-master-key-do-not-use-in-production-0001",
)


def _with_run_id(run_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": run_id, **result}


def create_app(db_path: str | Path = DEFAULT_DB,
               master_key: str = DEFAULT_MASTER_KEY) -> FastAPI:
    app = FastAPI(
        title="缓存键 / Vary 配置审计后端",
        version=__version__,
        description="本地响应元数据：分析哪些响应可共享，产出同键不同响应碰撞见证。",
    )
    storage = AuditStorage(db_path, master_key)
    app.state.storage = storage

    @app.exception_handler(AuditError)
    async def _audit_error_handler(_: Request, exc: AuditError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        clean_errors = [
            {
                "loc": list(err.get("loc", [])),
                "type": err.get("type", "value_error"),
                "msg": err.get("msg", ""),
            }
            for err in exc.errors()
        ]
        body = {
            "error": {
                "category": "input_error",
                "code": "request_validation",
                "message": "请求体或参数不合法",
                "details": {"errors": clean_errors},
            }
        }
        return JSONResponse(status_code=422, content=body)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # ---------- 运行 ----------
    @app.post("/v1/runs", response_model=RunView, status_code=201)
    async def create_run(req: CreateRunRequest) -> dict[str, Any]:
        return storage.create_run(req.run_id, req.label)

    @app.get("/v1/runs", response_model=list[RunView])
    async def list_runs(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return storage.list_runs(limit, offset)

    @app.get("/v1/runs/{run_id}", response_model=RunView)
    async def get_run(run_id: str) -> dict[str, Any]:
        return storage.get_run(run_id)

    @app.post("/v1/runs/{run_id}/seal", response_model=RunView)
    async def seal_run(run_id: str) -> dict[str, Any]:
        return storage.seal_run(run_id)

    # ---------- 策略 ----------
    @app.put("/v1/runs/{run_id}/policy", response_model=RunView)
    async def set_policy(run_id: str, policy: Policy) -> dict[str, Any]:
        return storage.set_policy(run_id, policy)

    @app.get("/v1/runs/{run_id}/policy")
    async def get_policy(run_id: str) -> dict[str, Any]:
        return storage.get_policy(run_id).model_dump(mode="json")

    # ---------- 证据 ----------
    @app.post("/v1/runs/{run_id}/evidence", response_model=RunView, status_code=201)
    async def add_evidence(run_id: str, batch: EvidenceBatch) -> dict[str, Any]:
        if not batch.evidence:
            from .errors import InputError
            raise InputError("evidence 批次不能为空",
                             code=ErrorCode.EVIDENCE_INVALID)
        storage.add_evidence(run_id, batch.evidence)
        return storage.get_run(run_id)

    @app.get("/v1/runs/{run_id}/evidence")
    async def list_evidence(run_id: str) -> list[dict[str, Any]]:
        return [e.model_dump(mode="json") for e in storage.list_evidence(run_id)]

    # ---------- 分析 / 修复 ----------
    @app.post("/v1/runs/{run_id}/analyze", response_model=AnalysisView)
    async def analyze(run_id: str) -> dict[str, Any]:
        from .errors import StateConflictError
        policy = storage.get_policy(run_id)
        evidence = storage.list_evidence(run_id)
        if not evidence:
            raise StateConflictError(
                "没有证据可分析（至少提交一条）", code=ErrorCode.NOTHING_TO_ANALYZE)
        result = kernel_analyze(policy, evidence)
        result = _with_run_id(run_id, result)
        storage.save_analysis(run_id, "current", result)
        return result

    @app.get("/v1/runs/{run_id}/analysis", response_model=AnalysisView)
    async def get_analysis(run_id: str) -> dict[str, Any]:
        return storage.get_analysis(run_id, "current")

    @app.post("/v1/runs/{run_id}/remediate", response_model=RemediationView)
    async def remediate(run_id: str) -> dict[str, Any]:
        from .errors import StateConflictError
        policy = storage.get_policy(run_id)
        evidence = storage.list_evidence(run_id)
        if not evidence:
            raise StateConflictError(
                "没有证据可修复验证", code=ErrorCode.NOTHING_TO_ANALYZE)
        result = kernel_remediate(policy, evidence)
        result = {"run_id": run_id, **result}
        result["before"] = _with_run_id(run_id, result["before"])
        result["after"] = _with_run_id(run_id, result["after"])
        storage.save_analysis(run_id, "current", result["before"])
        storage.save_analysis(run_id, "remediated", result["after"])
        storage.save_remediation_event(run_id, result)
        return result

    # ---------- 审计日志 ----------
    @app.get("/v1/runs/{run_id}/events", response_model=list[EventView])
    async def list_events(run_id: str) -> list[dict[str, Any]]:
        return storage.read_events(run_id)

    @app.get("/v1/runs/{run_id}/verify", response_model=VerifyView)
    async def verify_chain(run_id: str) -> dict[str, Any]:
        return storage.verify_chain(run_id)

    return app


app = create_app()
