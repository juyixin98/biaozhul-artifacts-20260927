"""错误类别。

每类错误有稳定的 code（供 API 与测试断言具体失败类别）与对应的 HTTP 状态。
不允许把异常或未知状态统一包装成成功：未知异常会保留为 internal_error。
"""
from __future__ import annotations


class Merge3Error(Exception):
    """所有领域错误的基类。"""

    code: str = "internal_error"
    http_status: int = 500


class NotFoundError(Merge3Error):
    code = "not_found"
    http_status = 404


class ConflictError(Merge3Error):
    """请求与当前状态冲突（分支已前进、合并已提交等）。"""

    code = "conflict"
    http_status = 409


class ValidationError(Merge3Error):
    """输入不满足结构约束（缺主键、Schema 不兼容、空主键等）。"""

    code = "validation_error"
    http_status = 422


class UnresolvedConflictError(Merge3Error):
    """提交合并时仍存在未解决冲突。"""

    code = "unresolved_conflicts"
    http_status = 409


class BindingMismatchError(Merge3Error):
    """解决方案与开启该合并时的三方快照绑定不一致。"""

    code = "resolution_binding_mismatch"
    http_status = 409


class MergeAlreadyCommittedError(Merge3Error):
    code = "merge_already_committed"
    http_status = 409


class ResolutionRejectedError(Merge3Error):
    """给定的解决方案不适用于该冲突类别（如对取值冲突给 DELETE 却没给行）。"""

    code = "resolution_rejected"
    http_status = 422
