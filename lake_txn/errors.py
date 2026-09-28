"""领域错误码与异常类型。

失败必须归类到稳定的 reason_code，调用方据此决定重试/刷新/放弃，
而不是靠解析错误文本。
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """所有可预期的业务失败。"""

    def __init__(
        self,
        status_code: int,
        reason_code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason_code = reason_code
        self.message = message
        self.detail = detail or {}


# ---- 失败类别（reason_code）----
# 资源类
UNKNOWN_TABLE = "UNKNOWN_TABLE"
UNKNOWN_BASE = "UNKNOWN_BASE"
BASE_IN_FUTURE = "BASE_IN_FUTURE"
# 冲突类（提交被拒绝，调用方应刷新快照后重试或放弃）
PARTITION_CONFLICT = "PARTITION_CONFLICT"
CONCURRENT_OVERWRITE = "CONCURRENT_OVERWRITE"
STALE_BASE_OVERWRITE = "STALE_BASE_OVERWRITE"
REQUEST_SCOPE_MISMATCH = "REQUEST_SCOPE_MISMATCH"
# 文件/暂存类
FILE_NOT_STAGED = "FILE_NOT_STAGED"
FILE_HASH_MISMATCH = "FILE_HASH_MISMATCH"
FILE_PARTITION_MISMATCH = "FILE_PARTITION_MISMATCH"
STAGE_VALIDATION_FAILED = "STAGE_VALIDATION_FAILED"
PUBLISH_FAILURE = "PUBLISH_FAILURE"
# 模式/请求类
SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
VALIDATION_ERROR = "VALIDATION_ERROR"
UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
IMPORT_PATH_FORBIDDEN = "IMPORT_PATH_FORBIDDEN"


def bad_request(code: str, message: str, detail: dict[str, Any] | None = None) -> DomainError:
    return DomainError(400, code, message, detail)


def rejected(code: str, message: str, detail: dict[str, Any] | None = None) -> DomainError:
    """确定性拒绝（409）：冲突已判定，结果明确。"""
    return DomainError(409, code, message, detail)


def unprocessable(code: str, message: str, detail: dict[str, Any] | None = None) -> DomainError:
    return DomainError(422, code, message, detail)
