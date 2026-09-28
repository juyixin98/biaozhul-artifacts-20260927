"""FastAPI 应用工厂：路由、请求身份中间件、统一错误信封。"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .. import __version__
from ..encoding import hex_to_bytes, to_checksum_address
from ..errors import BAD_REQUEST, PoolError
from ..logging_setup import get_logger
from ..service import Service
from . import routes


def create_app(service: Service) -> FastAPI:
    app = FastAPI(
        title="local-txpool",
        version=__version__,
        description="本地账户交易池：nonce 前缀、费用替换、容量/过期、区块确认与回滚",
    )
    app.state.service = service
    app.state.log = get_logger()

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or service.new_request_id()
        request.state.request_id = request_id
        app.state.log.info("http.start", extra={
            "request_id": request_id,
            "action": f"{request.method} {request.url.path}",
            "component": "api",
        })
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Service-Version"] = __version__
        return response

    @app.exception_handler(PoolError)
    async def pool_error_handler(request: Request, exc: PoolError):
        request_id = getattr(request.state, "request_id", "")
        app.state.log.warning("request failed", extra={
            "request_id": request_id, "code": exc.code, "action": "pool_error",
            "component": "api"})
        return JSONResponse(
            status_code=exc.http_status,
            content={"ok": False, "error": exc.code, "message": str(exc),
                     "details": exc.details, "request_id": request_id,
                     "warnings": []},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        request_id = getattr(request.state, "request_id", "")
        return JSONResponse(
            status_code=422,
            content={"ok": False, "error": BAD_REQUEST,
                     "message": "request validation failed",
                     "details": {"validation": exc.errors()},
                     "request_id": request_id, "warnings": []},
        )

    app.include_router(routes.router)
    return app
