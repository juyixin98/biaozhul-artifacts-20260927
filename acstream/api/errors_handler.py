"""统一错误响应与诊断落库。

错误体结构::

    {"error": {"code", "message", "request_id", "outcome", "details"}}

业务错误（ApiError）全部带 outcome：reject 表示可确定地拒绝；
undetermined 表示服务端无法判定状态一致性、需要客户端在显式边界澄清。
所有拒绝/无法判定事件在此统一写诊断记录（成功事件由服务层记录），
保证“为什么拒绝”一定有迹可循。
"""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..diagnostics import get_request_id
from ..errors import ApiError, ErrorCode


def _error_body(code: str, message: str, outcome: str, details: dict) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": get_request_id(),
            "outcome": outcome,
            "details": details,
        }
    }


def register_exception_handlers(app) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        deps = getattr(request.app.state, "deps", None)
        sid = request.path_params.get("sid")
        version_id = request.path_params.get("version_id")
        if deps is not None:
            try:
                with deps.lock:
                    deps.diagnostics.record(
                        outcome=exc.outcome,
                        code=exc.code,
                        message=exc.message,
                        key_state={
                            "path": request.url.path,
                            "method": request.method,
                            "details": exc.details,
                        },
                        sid=sid,
                        version_id=version_id,
                    )
            except Exception:  # noqa: BLE001 - 诊断失败不影响错误响应
                pass
        return JSONResponse(
            status_code=exc.http_status,
            content=_error_body(
                exc.code, exc.message, exc.outcome, exc.details
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 提取错误位置，但不回显输入值（可能含敏感载荷）。
        fields = []
        for err in exc.errors():
            loc = [str(x) for x in err.get("loc", []) if x != "body"]
            fields.append(
                {
                    "location": ".".join(loc) or "(body)",
                    "type": err.get("type"),
                    "msg": err.get("msg"),
                }
            )
        deps = getattr(request.app.state, "deps", None)
        if deps is not None:
            try:
                with deps.lock:
                    deps.diagnostics.record(
                        outcome="reject",
                        code=ErrorCode.VALIDATION_ERROR,
                        message="请求体/参数未通过结构校验",
                        key_state={
                            "path": request.url.path,
                            "method": request.method,
                            "fields": fields,
                        },
                    )
            except Exception:  # noqa: BLE001
                pass
        return JSONResponse(
            status_code=422,
            content=_error_body(
                ErrorCode.VALIDATION_ERROR,
                "请求结构或字段类型不合法（输入值未回显）",
                "reject",
                {"fields": fields},
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        deps = getattr(request.app.state, "deps", None)
        if deps is not None:
            try:
                with deps.lock:
                    deps.diagnostics.record(
                        outcome="undetermined",
                        code=ErrorCode.INTERNAL_ERROR,
                        # 异常类型可定位问题；不记录堆栈中的参数值。
                        message=f"未预期异常：{type(exc).__name__}",
                        key_state={
                            "path": request.url.path,
                            "method": request.method,
                            "exception_type": type(exc).__name__,
                        },
                    )
            except Exception:  # noqa: BLE001
                pass
        return JSONResponse(
            status_code=500,
            content=_error_body(
                ErrorCode.INTERNAL_ERROR,
                "服务内部错误，已记录诊断事件；请求未保证被处理，请在显式边界核对状态后重试",
                "undetermined",
                {},
            ),
        )
