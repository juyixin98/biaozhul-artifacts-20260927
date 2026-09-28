"""统一错误模型。

四类失败必须可区分（见 README“边界语义”）：
- VALIDATION_ERROR   输入错误（HTTP 400）
- STATE_CONFLICT     状态冲突（HTTP 409），如父快照过期、位置删除指向已重写文件
- RESOURCE_EXHAUSTED 资源耗尽（HTTP 413），如行数超过表配置上限
- COMPUTATION_FAILED 计算失败 / 数据完整性失败（HTTP 500）
另设 NOT_FOUND（HTTP 404）。
所有错误以统一信封返回：
    {"error": {"category", "code", "message", "details"}, "run_id": ...}
"""
from __future__ import annotations

from typing import Any


class ErrorCategory:
    VALIDATION_ERROR = "VALIDATION_ERROR"
    STATE_CONFLICT = "STATE_CONFLICT"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    COMPUTATION_FAILED = "COMPUTATION_FAILED"
    NOT_FOUND = "NOT_FOUND"


_HTTP_STATUS = {
    ErrorCategory.VALIDATION_ERROR: 400,
    ErrorCategory.STATE_CONFLICT: 409,
    ErrorCategory.RESOURCE_EXHAUSTED: 413,
    ErrorCategory.COMPUTATION_FAILED: 500,
    ErrorCategory.NOT_FOUND: 404,
}


class AppError(Exception):
    category: str = ErrorCategory.COMPUTATION_FAILED
    http_status: int = 500

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def envelope(self, run_id: str | None = None) -> dict[str, Any]:
        return {
            "error": {
                "category": self.category,
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
            "run_id": run_id,
        }


class ValidationError(AppError):
    category = ErrorCategory.VALIDATION_ERROR
    http_status = 400


class StateConflict(AppError):
    category = ErrorCategory.STATE_CONFLICT
    http_status = 409


class ResourceExhausted(AppError):
    category = ErrorCategory.RESOURCE_EXHAUSTED
    http_status = 413


class ComputationFailed(AppError):
    category = ErrorCategory.COMPUTATION_FAILED
    http_status = 500


class NotFound(AppError):
    category = ErrorCategory.NOT_FOUND
    http_status = 404


def http_status_for(category: str) -> int:
    return _HTTP_STATUS.get(category, 500)
