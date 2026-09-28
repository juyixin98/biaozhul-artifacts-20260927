"""统一错误契约。

所有跨模块、跨接口边界抛出的错误都使用这里定义的类别。
错误类别字符串是对外稳定契约（API 响应体的 ``error.category`` 字段）：

- input_error      输入错误（400）：请求本身不合法
- not_found        资源不存在（404）
- state_conflict   状态冲突（409）：请求合法，但与当前/绑定版本状态矛盾
- resource_limit   资源耗尽（413）：超出本地合成环境的硬性配额
- compute_failure  计算失败（500）：执行内核无法完成计算

每一类都代表*不同的处置方式*，测试会分别断言。
"""
from __future__ import annotations

from typing import Any


class DeleterError(Exception):
    """所有领域错误的基类。"""

    category: str = "compute_failure"
    http_status: int = 500

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "category": self.category,
                "message": self.message,
                "details": self.details,
            }
        }


class InputError(DeleterError):
    """请求不合法：字段缺失、取值越界、格式不支持等。"""

    category = "input_error"
    http_status = 400


class NotFoundError(DeleterError):
    """表、文件、运行编号等资源不存在。"""

    category = "not_found"
    http_status = 404


class StateConflictError(DeleterError):
    """请求合法但与状态矛盾：版本过期、重复 ID 语义不同、并发修改等。"""

    category = "state_conflict"
    http_status = 409


class ResourceLimitError(DeleterError):
    """超过本地配额（单次载入行数/批大小/磁盘上限）。

    与输入错误区分：输入本身结构合法，只是资源预算不允许。
    """

    category = "resource_limit"
    http_status = 413


class ComputeFailureError(DeleterError):
    """执行内核在计算过程中失败（类型无法比较、底层数据损坏等）。"""

    category = "compute_failure"
    http_status = 500
