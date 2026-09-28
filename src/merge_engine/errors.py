"""错误契约：整个后端唯一的错误分类体系。

四类可区分的失败（题目要求“输入错误 / 状态冲突 / 资源耗尽 / 计算失败”可区分）：

| 类别          | HTTP | 含义                                            | 例子 |
|---------------|------|-------------------------------------------------|------|
| INPUT_ERROR   | 400  | 调用方输入/配置本身不合法，重试同输入无意义      | 源缺列、源内同键多行、条件引用了不存在的列 |
| STATE_CONFLICT| 409  | 输入合法，但目标当前状态使操作无法满足契约        | 目标快照内同键多行（脏目标） |
| RESOURCE_EXHAUSTED | 507 | 动作数/字节数/存储容量等资源限制被触发        | 计划超 max_actions、磁盘满 |
| COMPUTATION_FAILURE | 500 | 内核执行/提交阶段失败，系统侧问题            | 提交注入失败、SQLite 磁盘 IO 错误 |

每个异常都带稳定的 ``code``（程序断言用）和 ``details``（全部 JSON 可序列化）。
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    INPUT_ERROR = "INPUT_ERROR"
    STATE_CONFLICT = "STATE_CONFLICT"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    COMPUTATION_FAILURE = "COMPUTATION_FAILURE"


class MergeError(Exception):
    """所有 MERGE 相关异常的基类。"""

    category: ErrorCategory = ErrorCategory.COMPUTATION_FAILURE
    code: str = "COMPUTATION_FAILURE"
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "code": self.code,
            "message": str(self.args[0]) if self.args else "",
            "details": self.details,
        }


# ---- 输入错误（400） -------------------------------------------------------

class InputError(MergeError):
    category = ErrorCategory.INPUT_ERROR
    code = "INPUT_ERROR"
    http_status = 400


class SourceFormatError(InputError):
    code = "SOURCE_FORMAT_ERROR"


class SchemaMismatchError(InputError):
    code = "SCHEMA_MISMATCH"


class SourceDuplicateKeyError(InputError):
    """源内同键多行——按契约拒绝。details.duplicates 给出全部冲突。"""

    code = "SOURCE_DUPLICATE_KEY"

    def __init__(self, duplicates: list[dict[str, Any]], message: str | None = None) -> None:
        super().__init__(
            message or f"source contains {len(duplicates)} duplicate composite key(s)",
            details={"duplicates": duplicates},
        )


class KeyNullRejectedError(InputError):
    """SQL 语义下键列为 NULL 的源行不允许（NULL != NULL，无法定位键）。"""

    code = "KEY_NULL_REJECTED"


class ConfigError(InputError):
    code = "CONFIG_INVALID"


# ---- 状态冲突（409） -------------------------------------------------------

class StateConflict(MergeError):
    category = ErrorCategory.STATE_CONFLICT
    code = "STATE_CONFLICT"
    http_status = 409


class TargetDuplicateKeyError(StateConflict):
    """目标快照内同键多行：引擎拒绝猜测要更新哪一行。"""

    code = "TARGET_DUPLICATE_KEY"

    def __init__(self, duplicates: list[dict[str, Any]]) -> None:
        super().__init__(
            f"target snapshot contains {len(duplicates)} duplicate composite key(s)",
            details={"duplicates": duplicates},
        )


# ---- 资源耗尽（507） -------------------------------------------------------

class ResourceExhausted(MergeError):
    category = ErrorCategory.RESOURCE_EXHAUSTED
    code = "RESOURCE_EXHAUSTED"
    http_status = 507


class PlanTooLargeError(ResourceExhausted):
    code = "PLAN_TOO_LARGE"


class DiskFullError(ResourceExhausted):
    code = "DISK_FULL"


# ---- 计算失败（500） -------------------------------------------------------

class ComputationFailure(MergeError):
    category = ErrorCategory.COMPUTATION_FAILURE
    code = "COMPUTATION_FAILURE"
    http_status = 500


class CommitFailure(ComputationFailure):
    """提交阶段失败。原子性保证：此异常出现后目标表必须保持操作前状态。"""

    code = "COMMIT_FAILED"


class ConditionEvaluationFailure(ComputationFailure):
    code = "CONDITION_EVALUATION_FAILED"


CATEGORY_HTTP = {
    ErrorCategory.INPUT_ERROR: 400,
    ErrorCategory.STATE_CONFLICT: 409,
    ErrorCategory.RESOURCE_EXHAUSTED: 507,
    ErrorCategory.COMPUTATION_FAILURE: 500,
}
