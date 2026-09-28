"""FastAPI 应用工厂与路由。

鉴权
----
- 数据接口：每个运行创建时返回的 ``X-Run-Token``（只存摘要）。
- 审计接口：管理令牌 ``X-Admin-Token``（来自环境变量）；临时模式下服务
  启动时生成一次性管理令牌并打印告警，生产必须显式配置。

错误不吞没：:class:`RiskError` 映射为对应 HTTP 状态 + 稳定错误码；未知
异常映射为 500 ``INTERNAL_ERROR`` 且带关联 ID，绝不返回成功。
"""

from __future__ import annotations

import csv as _csv
import io
import os
import secrets
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import Settings, load_settings
from ..errors import ErrorCode, RiskError
from ..logging_setup import configure_logging, get_logger, log_context
from ..security import KeyManager
from ..service import RunService
from ..storage import AuditLog, RunStore
from .schemas import (
    CreateRunIn, HealthOut, LevelsIn, RunOut, SuggestIn, ThresholdIn,
)

log = get_logger("api")


def _correlation_id(request: Request) -> str:
    cid = request.headers.get("X-Correlation-ID")
    if cid:
        return cid[:64]
    return "req-" + secrets.token_hex(8)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings)
    keys = KeyManager.from_settings(settings)
    store = RunStore(
        os.path.join(settings.storage.data_dir, settings.storage.runs_subdir),
        keys,
    )
    audit = AuditLog(settings.storage.audit_db)
    service = RunService(settings, keys, store, audit)

    admin_token = os.environ.get(settings.security.admin_token_env)
    if not admin_token:
        admin_token = "ephemeral-" + secrets.token_urlsafe(16)
        log.warning(
            "未配置管理令牌，已生成一次性管理令牌（仅本进程有效）",
            extra={"event": {"admin_token_source": "ephemeral",
                             "hint": "用 ANON_RISK_ADMIN_TOKEN 配置固定令牌"}},
        )

    app = FastAPI(
        title="anon-risk",
        version=__version__,
        description="匿名化等价类风险检查：k-匿名 / l-多样性核验与泛化建议（合成数据）",
    )
    app.state.settings = settings
    app.state.service = service
    app.state.keys = keys

    # ---- 异常映射：未知状态不返回成功 -------------------------------
    @app.exception_handler(RiskError)
    async def risk_error_handler(request: Request, exc: RiskError):
        cid = getattr(request.state, "correlation_id", None)
        log.warning(
            "业务失败 %s: %s", exc.code.value, exc.message,
            extra={"event": {"error_code": exc.code.value,
                             "correlation_id": cid}},
        )
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code.value,
                               "message": exc.message,
                               "details": exc.details,
                               "correlation_id": cid}},
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        cid = getattr(request.state, "correlation_id", None)
        log.exception("未处理异常", extra={"event": {"correlation_id": cid}})
        return JSONResponse(
            status_code=500,
            content={"error": {"code": ErrorCode.INTERNAL_ERROR.value,
                               "message": "内部错误，请凭 correlation_id 联系管理员",
                               "correlation_id": cid}},
        )

    @app.middleware("http")
    async def attach_context(request: Request, call_next):
        cid = _correlation_id(request)
        request.state.correlation_id = cid
        token = log_context(correlation_id=cid)
        token.__enter__()
        try:
            response = await call_next(request)
        finally:
            token.__exit__(None, None, None)
        response.headers["X-Correlation-ID"] = cid
        return response

    # ---- 依赖 --------------------------------------------------------
    def _cid(request: Request) -> str:
        return request.state.correlation_id

    def _require_admin(x_admin_token: Optional[str] = Header(default=None)):
        if not x_admin_token or not secrets.compare_digest(
                x_admin_token, admin_token):
            raise RiskError(
                "管理令牌缺失或不匹配",
                code=ErrorCode.UNAUTHORIZED, http_status=401,
            )
        return True

    # ---- 健康 / 版本 -------------------------------------------------
    @app.get("/health", response_model=HealthOut, tags=["meta"])
    async def health():
        return HealthOut(
            status="ok", service=settings.app.name, version=__version__,
            metric_version=settings.app.metric_version,
            key_ephemeral=keys.ephemeral,
            config_source=settings.source_path,
            run_count=len(service.list_runs()),
        )

    @app.get("/version", tags=["meta"])
    async def version():
        return {
            "service": settings.app.name,
            "version": __version__,
            "metric_version": settings.app.metric_version,
            "schema_version": settings.app.schema_version,
            "key_ephemeral": keys.ephemeral,
        }

    # ---- 运行 --------------------------------------------------------
    @app.post("/runs", response_model=RunOut, status_code=201, tags=["runs"])
    async def create_run(body: CreateRunIn, request: Request):
        payload = body.model_dump(by_alias=True)
        result = service.create_run(
            payload, correlation_id=request.state.correlation_id)
        return result

    @app.post("/runs/csv", response_model=RunOut, status_code=201, tags=["runs"])
    async def create_run_csv(
        request: Request,
        file: UploadFile = File(..., description="UTF-8 CSV，首行表头，空单元格=NULL"),
        quasi_identifiers: str = Form(..., description="逗号分隔的 QI 列名"),
        sensitive: str = Form(..., description="逗号分隔的敏感列名"),
        hierarchies: str = Form(...,
            description='JSON，形如 {"zip":{"levels":[{"rule":"prefix","keep":2}]}}'),
    ):
        import json
        text = (await file.read()).decode("utf-8-sig")
        reader = _csv.reader(io.StringIO(text))
        try:
            header = next(reader)
        except StopIteration:
            raise RiskError("CSV 为空或缺少表头",
                            code=ErrorCode.HEADER_MISSING_COLUMNS)
        rows = [row for row in reader]
        try:
            hier = json.loads(hierarchies)
        except json.JSONDecodeError as exc:
            raise RiskError("hierarchies 不是合法 JSON",
                            code=ErrorCode.INVALID_PARAMETER) from exc
        payload = {
            "columns": [h.strip() for h in header],
            "rows": rows,
            "quasi_identifiers": [c.strip() for c in quasi_identifiers.split(",") if c.strip()],
            "sensitive": [c.strip() for c in sensitive.split(",") if c.strip()],
            "hierarchies": hier,
        }
        return service.create_run(payload,
                                  correlation_id=request.state.correlation_id)

    def _check_run(run_id: str, x_run_token: Optional[str]):
        if not x_run_token:
            raise RiskError("缺少 X-Run-Token",
                            code=ErrorCode.UNAUTHORIZED, http_status=401)
        if not service.store.exists(run_id):
            raise RiskError("运行不存在", code=ErrorCode.RUN_NOT_FOUND,
                            http_status=404)
        return x_run_token

    @app.get("/runs", tags=["runs"])
    async def list_runs():
        return {"runs": service.list_runs()}

    @app.get("/runs/{run_id}", tags=["runs"])
    async def get_run(run_id: str, request: Request):
        if not service.store.exists(run_id):
            raise RiskError("运行不存在", code=ErrorCode.RUN_NOT_FOUND,
                            http_status=404)
        return service.get_run(run_id)

    @app.delete("/runs/{run_id}", status_code=204, tags=["runs"])
    async def delete_run(run_id: str, request: Request,
                         x_run_token: Optional[str] = Header(default=None)):
        token = _check_run(run_id, x_run_token)
        service.delete_run(run_id, token,
                           correlation_id=request.state.correlation_id)

    @app.get("/runs/{run_id}/operations", tags=["runs"])
    async def run_operations(run_id: str, request: Request,
                             x_run_token: Optional[str] = Header(default=None)):
        token = _check_run(run_id, x_run_token)
        return service.operations(run_id, token)

    # ---- 风险核验 / 建议 --------------------------------------------
    @app.post("/runs/{run_id}/evaluate", tags=["risk"])
    async def evaluate(run_id: str, body: LevelsIn, request: Request,
                       threshold: ThresholdIn = Depends(),
                       x_run_token: Optional[str] = Header(default=None)):
        token = _check_run(run_id, x_run_token)
        return service.evaluate(
            run_id, token, body.levels, threshold.k, threshold.l,
            correlation_id=request.state.correlation_id,
        )

    @app.post("/runs/{run_id}/suggest", tags=["risk"])
    async def suggest(run_id: str, body: SuggestIn, request: Request,
                      x_run_token: Optional[str] = Header(default=None)):
        token = _check_run(run_id, x_run_token)
        return service.suggest(
            run_id, token, body.k, body.l,
            correlation_id=request.state.correlation_id,
        )

    # ---- 审计（管理） ------------------------------------------------
    @app.get("/audit/events", tags=["audit"],
             dependencies=[Depends(_require_admin)])
    async def audit_events(
        request: Request,
        run_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ):
        return service.audit_events(
            run_id=run_id, limit=limit, offset=offset, status=status,
            correlation_id=request.state.correlation_id,
        )

    return app


def get_app() -> FastAPI:
    """uvicorn 入口：``anon_risk.api.main:app``。"""
    return create_app()


app = get_app()
