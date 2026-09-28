"""错误分类与统一错误信封。

四类可区分错误（需求：输入错误、状态冲突、资源耗尽、计算失败）：

- INPUT_INVALID       422  输入数据/参数不合法（含截断、非帧对齐、坏 WAV）
- STATE_CONFLICT      409  作业状态不允许该操作（finalized 后写块等）
- RESOURCE_EXHAUSTED  413  样本数 / 请求体 / 作业数达到上限
- JOB_NOT_FOUND       404  作业不存在
- COMPUTATION_FAILED  500  内核/存储内部失败
"""
from __future__ import annotations

from typing import Any


class SegmentError(Exception):
    """所有可预期业务错误的基类。"""

    code = "COMPUTATION_FAILED"
    http_status = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class InputInvalidError(SegmentError):
    code = "INPUT_INVALID"
    http_status = 422


class StateConflictError(SegmentError):
    code = "STATE_CONFLICT"
    http_status = 409


class ResourceExhaustedError(SegmentError):
    code = "RESOURCE_EXHAUSTED"
    http_status = 413


class JobNotFoundError(SegmentError):
    code = "JOB_NOT_FOUND"
    http_status = 404


class ComputationFailedError(SegmentError):
    code = "COMPUTATION_FAILED"
    http_status = 500


def error_envelope(code: str, message: str, run_id: str,
                   details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "run_id": run_id,
            "details": details or {},
        }
    }
