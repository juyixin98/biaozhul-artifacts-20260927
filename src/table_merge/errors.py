"""领域异常与错误码。

错误码以字符串常量暴露给 HTTP 层，绝不把未知/异常状态吞掉后返回成功。
"""
from __future__ import annotations


class MergeError(Exception):
    """所有可预期业务错误的基类。"""

    error_code = "MERGE_ERROR"
    http_status = 400

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error_code": self.error_code, "message": self.message, "details": self.details}


class NotFoundError(MergeError):
    error_code = "NOT_FOUND"
    http_status = 404


class ConflictStateError(MergeError):
    """对象存在但状态不允许该操作（分支已存在、仍有未解决冲突等）。"""

    error_code = "CONFLICT_STATE"
    http_status = 409


class SchemaMismatchError(MergeError):
    error_code = "SCHEMA_MISMATCH"
    http_status = 422


class InvalidPayloadError(MergeError):
    error_code = "INVALID_PAYLOAD"
    http_status = 422


class InvalidResolutionError(MergeError):
    error_code = "INVALID_RESOLUTION"
    http_status = 422


class SnapshotFormatError(MergeError):
    error_code = "SNAPSHOT_FORMAT"
    http_status = 422


class AncestryError(MergeError):
    """两个引用没有共同祖先，无法做三方合并。"""

    error_code = "NO_COMMON_ANCESTOR"
    http_status = 422
