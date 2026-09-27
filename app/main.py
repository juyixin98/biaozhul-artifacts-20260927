"""FastAPI 应用工厂与启动入口。

运行：
    uvicorn app.main:app --reload
或：
    python -m app.main
"""
from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api.routes import router
from .api.service_error import ServiceError
from .config import get_settings
from .diagnostics.errors import error_envelope
from .diagnostics.logging_setup import JsonlLogger
from .diagnostics.tracer import new_request_id
from .storage.version_store import VersionStore


def create_app(db_path: str | None = None, log_dir: str | None = None) -> FastAPI:
    settings = get_settings()
    settings.ensure_dirs()

    app = FastAPI(
        title="持久 Posting 列表布尔查询服务",
        version="1.0.0",
        description=(
            "在版本化文档全集上执行 AND/OR/NOT 与短路求值；"
            "跳跃块上界仅用于安全跳过；NOT 相对显式有限全集求补。"
        ),
    )
    app.state.db_path = db_path or settings.db_path
    app.state.log_dir = log_dir or settings.log_dir
    app.state.store = VersionStore(app.state.db_path, block_size=settings.block_size)
    app.state.json_log = JsonlLogger(app.state.log_dir)

    # ------------------------------------------------------------------
    # 中间件：为每个请求关联身份（X-Request-ID 可由客户端指定）
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or new_request_id()
        request.state.request_id = rid
        request.state.started_at = time.time()
        app.state.json_log.request_start(
            rid, request.method, request.url.path, str(request.url.query)
        )
        try:
            response = await call_next(request)
        except Exception:
            app.state.json_log.request_done(
                rid,
                status="error",
                version=None,
                expression=None,
                result_count=None,
                error_category="unhandled_exception",
                error_message="请求处理抛出未捕获异常",
            )
            raise
        response.headers["X-Request-ID"] = rid
        return response

    # ------------------------------------------------------------------
    # 统一错误信封
    # ------------------------------------------------------------------
    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        rid = getattr(request.state, "request_id", exc.request_id or "-")
        body = error_envelope(
            rid,
            exc.category,
            exc.message,
            expression=exc.expression,
            version=exc.version,
            position=exc.position,
            uncertainty=exc.uncertainty,
        )
        return JSONResponse(status_code=exc.http_status, content=body)

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        body = error_envelope(
            rid,
            "internal_error",
            f"{type(exc).__name__}: {exc}",
        )
        return JSONResponse(status_code=500, content=body)

    app.include_router(router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
