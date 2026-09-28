"""FastAPI 应用入口与全局错误处理。

启动::

    ANON_ALLOW_EPHEMERAL_KEY=1 .venv/bin/uvicorn app.main:app --reload

错误语义
========
* 业务错误 :class:`ServiceError` → 结构化 error 体 + 对应状态码；
* 请求体校验错误（422）→ 同构错误体，类别 validation；
* 未预期异常 → 500 + INTERNAL_ERROR，记录 error 级审计，绝不返回成功。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import router
from app.config import Settings
from app.core.errors import CATEGORY, FailureCode, ServiceError
from app.core.logging_setup import configure_logging, get_logger, log_event
from app.state import AppState, build_state


def create_app(settings: Settings | None = None, state: AppState | None = None) -> FastAPI:
    app = FastAPI(
        title="匿名化等价类风险检查服务",
        version=__version__,
        description=(
            "Synthetic-data backend for k-anonymity / l-diversity equivalence-class "
            "risk checks and information-loss-optimal generalization suggestions."
        ),
    )
    app.state.app_state = state or build_state(settings)
    configure_logging(app.state.app_state.settings.app_log_path or None)
    log = get_logger("api")

    app.include_router(router, tags=["risk"])

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
        body = exc.to_dict()
        # 业务失败也是被记录的明确结果，而不是成功
        if exc.code in (FailureCode.RUN_NOT_FOUND, FailureCode.SCHEMA_NOT_FOUND):
            log_event(log, "request_failed", level=30, code=exc.code.value, path=request.url.path)
        return JSONResponse(status_code=exc.http_status, content=body)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 归类字段错误，不回显请求体（敏感数据最小暴露）
        errs = []
        for e in exc.errors():
            errs.append(
                {
                    "location": [str(x) for x in e.get("loc", [])],
                    "rule": e.get("type", ""),
                    "message": e.get("msg", ""),
                }
            )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": FailureCode.INVALID_INPUT.value,
                    "category": CATEGORY[FailureCode.INVALID_INPUT],
                    "message": "request payload failed validation",
                    "details": {"validation_errors": errs},
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log_event(
            log,
            "internal_error",
            level=40,
            path=request.url.path,
            error_type=type(exc).__name__,
        )
        try:
            app.state.app_state.audit.append(
                "http_unhandled",
                "error",
                detail={"path": request.url.path, "error_type": type(exc).__name__},
            )
        except Exception:  # noqa: BLE001
            pass
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": FailureCode.INTERNAL_ERROR.value,
                    "category": CATEGORY[FailureCode.INTERNAL_ERROR],
                    "message": "unexpected internal error; logged with status=error",
                    "details": {},
                }
            },
        )

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {"service": "anon-risk", "version": __version__, "docs": "/docs"}

    return app


app = create_app()
