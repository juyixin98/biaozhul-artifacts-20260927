"""错误分类与数据契约。

四类错误彼此可区分（测试按 ``category`` 断言失败类别，而不只是状态码）：

  INPUT_ERROR          输入错误：操作形状/参数非法、索引越界、未知文档。400/404
  STATE_CONFLICT       状态冲突：版本陈旧、幂等键冲突、旧基线（已裁剪）。409/410
  RESOURCE_EXHAUSTED   资源耗尽：文档/操作超过配额。413
  COMPUTE_FAILED       计算失败：核心算法不变量被破坏（绝不应对正常输入发生）。500

所有错误都携带稳定的机器可读 ``code``，便于重放与分类断言。
"""
from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    INPUT_ERROR = "input_error"
    STATE_CONFLICT = "state_conflict"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    COMPUTE_FAILED = "compute_failed"


# (code -> category) 注册表
_REGISTRY: dict[str, ErrorCategory] = {}


class OTError(Exception):
    """所有 OT 业务错误的基类。"""

    category: ErrorCategory = ErrorCategory.COMPUTE_FAILED
    code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str = "", *, details: dict | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details: dict = details or {}

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "category": self.category.value,
                "message": self.message,
                "details": self.details,
            }
        }


def _register(category: ErrorCategory, code: str, http_status: int):
    _REGISTRY[code] = category

    def deco(cls):
        cls.category = category
        cls.code = code
        cls.http_status = http_status
        return cls

    return deco


# ---------------------------------------------------------------- 输入错误
class InputError(OTError):
    category = ErrorCategory.INPUT_ERROR
    http_status = 400


@_register(ErrorCategory.INPUT_ERROR, "doc_not_found", 404)
class DocumentNotFound(InputError):
    http_status = 404

    def __init__(self, doc_id: str):
        super().__init__(f"document not found: {doc_id}", details={"doc_id": doc_id})


@_register(ErrorCategory.INPUT_ERROR, "malformed_operation", 400)
class MalformedOperation(InputError):
    """组件形状非法：未知类型、负数、retain=0 等。"""


@_register(ErrorCategory.INPUT_ERROR, "invalid_index", 400)
class InvalidIndex(InputError):
    """字符索引越界或删除区间超出当前文本长度。"""


@_register(ErrorCategory.INPUT_ERROR, "empty_insert", 400)
class EmptyInsert(InputError):
    """插入空串或空操作（no-op）。"""


@_register(ErrorCategory.INPUT_ERROR, "unsupported_content", 400)
class UnsupportedContent(InputError):
    """文本不是合法的 Unicode 字符串（如孤立代理项）。"""


# ---------------------------------------------------------------- 状态冲突
class StateConflict(OTError):
    category = ErrorCategory.STATE_CONFLICT
    http_status = 409


@_register(ErrorCategory.STATE_CONFLICT, "stale_revision", 409)
class StaleRevision(StateConflict):
    def __init__(self, given: int, head: int):
        super().__init__(
            f"base revision {given} is behind head {head}",
            details={"given_revision": given, "head_revision": head},
        )


@_register(ErrorCategory.STATE_CONFLICT, "revision_ahead", 409)
class RevisionAhead(StateConflict):
    def __init__(self, given: int, head: int):
        super().__init__(
            f"base revision {given} is ahead of head {head}",
            details={"given_revision": given, "head_revision": head},
        )


@_register(ErrorCategory.STATE_CONFLICT, "duplicate_request", 409)
class DuplicateRequest(StateConflict):
    """同一个 ``Idempotency-Key`` 被重复提交，但请求体与首次不一致。

    完全一致的重复提交不算错误——服务返回首次结果（幂等成功）。
    """


@_register(ErrorCategory.STATE_CONFLICT, "stale_baseline", 410)
class StaleBaseline(StateConflict):
    """历史裁剪后，客户端所依据的旧版本已不存在，必须重建基线。"""

    http_status = 410

    def __init__(self, requested: int, oldest: int):
        super().__init__(
            f"revision {requested} older than pruned horizon {oldest}; "
            "rebuild baseline from the current snapshot",
            details={"requested_revision": requested, "oldest_revision": oldest},
        )


@_register(ErrorCategory.STATE_CONFLICT, "client_reset_required", 409)
class ClientResetRequired(StateConflict):
    """客户端检测到本地有未确认编辑时基线被重建，无法继续 OT，必须重置。"""


# ------------------------------------------------------------- 资源耗尽
@_register(ErrorCategory.RESOURCE_EXHAUSTED, "document_too_large", 413)
class DocumentTooLarge(OTError):
    category = ErrorCategory.RESOURCE_EXHAUSTED
    http_status = 413


@_register(ErrorCategory.RESOURCE_EXHAUSTED, "operation_too_large", 413)
class OperationTooLarge(OTError):
    category = ErrorCategory.RESOURCE_EXHAUSTED
    http_status = 413


# --------------------------------------------------------------- 计算失败
@_register(ErrorCategory.COMPUTE_FAILED, "transform_invariant", 500)
class TransformInvariant(OTError):
    """transform/apply 内部不变量被破坏。正常输入下永不出现；出现即实现缺陷。

    测试通过诊断注入点构造该错误，验证其与其它类别可区分。
    """

    category = ErrorCategory.COMPUTE_FAILED
    http_status = 500


@_register(ErrorCategory.COMPUTE_FAILED, "storage_failure", 500)
class StorageFailure(OTError):
    category = ErrorCategory.COMPUTE_FAILED
    http_status = 500


def category_of(code: str) -> ErrorCategory:
    return _REGISTRY.get(code, ErrorCategory.COMPUTE_FAILED)
