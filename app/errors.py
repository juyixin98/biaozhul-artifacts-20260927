"""统一错误分类与错误契约。

需求约定四类必须可区分的失败：
- input_error        输入错误（非法 UTF-8、越界位置、非边界位置、参数校验失败等）
- state_conflict     状态冲突（文档/版本不存在、版本号冲突、索引绑定摘要不一致等）
- resource_exhausted 资源耗尽（超过字节/码点/簇数量上限）
- computation_failure 计算失败（增量更新与完整重建不一致、Unicode 版本不匹配等）

每个错误携带稳定的机器可读 code，供测试断言“具体失败类别”，而非仅检查 HTTP 通断。
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    INPUT = "input_error"
    STATE = "state_conflict"
    RESOURCE = "resource_exhausted"
    COMPUTATION = "computation_failure"


class IndexServiceError(Exception):
    """所有业务错误的基类。"""

    category: ErrorCategory = ErrorCategory.INPUT
    code: str = "INDEX_SERVICE_ERROR"
    http_status: int = 400

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


# ── 输入错误 ────────────────────────────────────────────────────────────────
class InvalidBase64Error(IndexServiceError):
    category = ErrorCategory.INPUT
    code = "INVALID_BASE64"
    http_status = 400


class InvalidUtf8Error(IndexServiceError):
    """非法 UTF-8 字节序列。details 含首个非法字节偏移等定位信息。"""

    category = ErrorCategory.INPUT
    code = "INVALID_UTF8"
    http_status = 400


class EmptyFieldError(IndexServiceError):
    category = ErrorCategory.INPUT
    code = "EMPTY_FIELD"
    http_status = 400


class ValidationError(IndexServiceError):
    """请求结构/枚举值校验失败（与 pydantic 422 归一为同一信封）。"""

    category = ErrorCategory.INPUT
    code = "VALIDATION_ERROR"
    http_status = 422


class PositionOutOfRangeError(IndexServiceError):
    """位置超出 [0, 长度] 区间。"""

    category = ErrorCategory.INPUT
    code = "POSITION_OUT_OF_RANGE"
    http_status = 400


class NotABoundaryError(IndexServiceError):
    """位置合法但落在不允许的边界上（字节落在多字节序列内部、
    码点/字节落在扩展字素簇内部）。details 给出最近的合法边界。"""

    category = ErrorCategory.INPUT
    code = "NOT_A_BOUNDARY"
    http_status = 422


class UnsupportedSpaceError(IndexServiceError):
    category = ErrorCategory.INPUT
    code = "UNSUPPORTED_SPACE"
    http_status = 400


# ── 状态冲突 ────────────────────────────────────────────────────────────────
class DocumentNotFoundError(IndexServiceError):
    category = ErrorCategory.STATE
    code = "DOCUMENT_NOT_FOUND"
    http_status = 404


class VersionNotFoundError(IndexServiceError):
    category = ErrorCategory.STATE
    code = "VERSION_NOT_FOUND"
    http_status = 404


class VersionConflictError(IndexServiceError):
    """乐观锁：客户端持有的 expected_version 已不是当前版本。"""

    category = ErrorCategory.STATE
    code = "VERSION_CONFLICT"
    http_status = 409


class DocumentConflictError(IndexServiceError):
    """doc_id 已存在（创建冲突）。"""

    category = ErrorCategory.STATE
    code = "DOCUMENT_CONFLICT"
    http_status = 409


class IndexCorruptionError(IndexServiceError):
    """持久化索引与原文摘要绑定校验失败：摘要不一致或索引数组与原文重算不符。"""

    category = ErrorCategory.STATE
    code = "INDEX_CORRUPTION"
    http_status = 409


class UnicodeVersionMismatchError(IndexServiceError):
    """运行环境的 Unicode 数据版本与构建时钉住的版本不一致。"""

    category = ErrorCategory.STATE
    code = "UNICODE_VERSION_MISMATCH"
    http_status = 409


# ── 资源耗尽 ────────────────────────────────────────────────────────────────
class LimitExceededError(IndexServiceError):
    category = ErrorCategory.RESOURCE
    code = "LIMIT_EXCEEDED"
    http_status = 413


# ── 计算失败 ────────────────────────────────────────────────────────────────
class IndexInconsistentError(IndexServiceError):
    """增量编辑得到的索引与对新文本完整重建的索引不一致。"""

    category = ErrorCategory.COMPUTATION
    code = "INDEX_INCONSISTENT"
    http_status = 500


class SegmentationError(IndexServiceError):
    category = ErrorCategory.COMPUTATION
    code = "SEGMENTATION_FAILED"
    http_status = 500
