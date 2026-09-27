"""统一错误信封：失败原因（category）与不确定结论（uncertainty）永远单列。"""
from __future__ import annotations

from ..query.engine import (
    CAT_INTERNAL,
    CAT_ORDER,
    CAT_SPEC,
    CAT_STORAGE,
    CAT_VALIDATION,
    CAT_VERSION_NOT_FOUND,
)

# 失败类别 -> HTTP 状态码
HTTP_STATUS = {
    CAT_SPEC: 400,
    CAT_VALIDATION: 400,
    CAT_ORDER: 400,
    CAT_VERSION_NOT_FOUND: 404,
    CAT_STORAGE: 500,
    CAT_INTERNAL: 500,
}


def error_envelope(
    request_id: str,
    category: str,
    message: str,
    *,
    expression: str | None = None,
    version: int | None = None,
    position: int | None = None,
    uncertainty: list[str] | None = None,
) -> dict:
    return {
        "ok": False,
        "request_id": request_id,
        "expression": expression,
        "version": version,
        "error": {
            "category": category,
            "message": message,
            "position": position,
        },
        "uncertainty": uncertainty or [],
    }
