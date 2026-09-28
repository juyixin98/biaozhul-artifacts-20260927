"""错误分类法 —— 跨模块的错误契约。

内核所有可预期失败都抛出 :class:`KernelError` 的子类，并携带：

* ``category``    —— 五大粗分类，调用方据此映射 HTTP 状态与处理策略；
* ``code``        —— 稳定的细分类字符串，测试按它断言"失败类别"；
* ``details``     —— 结构化中间状态（运行编号、跳号、主机、命中规则等）；
* ``retryable``   —— 同一输入重试是否可能成功。

刻意不使用一个 ``Exception`` 加字符串消息：失败类别是安全产品的外部
契约，测试与审计都要按类别而非措辞判断。
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    INPUT_ERROR = "input_error"              # 输入本身不合法，同输入重试无意义
    POLICY_DENY = "policy_deny"              # 输入合法但被出站策略禁止
    STATE_CONFLICT = "state_conflict"        # 与既有运行状态冲突（重定向环等）
    RESOURCE_EXHAUSTED = "resource_exhausted"  # 预算耗尽（跳数、答案数、时间）
    COMPUTATION_FAILED = "computation_failed"  # 外部依赖或内部计算失败


class KernelError(Exception):
    """所有内核错误的基类。"""

    category: ErrorCategory = ErrorCategory.COMPUTATION_FAILED
    code: str = "E_INTERNAL"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        out = {
            "category": self.category.value,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
        reason = getattr(self, "reason", None)
        if reason is not None:
            out["reason"] = reason
        return out

    def __str__(self) -> str:  # pragma: no cover - 调试便利
        return f"[{self.category.value}/{self.code}] {self.message} {self.details}"


# ---------------------------------------------------------------------------
# 输入错误
# ---------------------------------------------------------------------------
class InputError(KernelError):
    category = ErrorCategory.INPUT_ERROR
    code = "E_INPUT"
    retryable = False


class PolicyFileError(InputError):
    """规则/证据文件本身不合法（schema、语法、插值）。"""

    code = "E_POLICY_FILE_INVALID"


class ZoneFileError(InputError):
    code = "E_ZONE_FILE_INVALID"


# ---------------------------------------------------------------------------
# 策略拒绝（携带决策链，见 kernel 中具体子类）
# ---------------------------------------------------------------------------
class PolicyDeniedError(KernelError):
    category = ErrorCategory.POLICY_DENY
    code = "E_POLICY_DENY"
    retryable = False

    def __init__(self, message: str, *, reason: str, details: dict[str, Any] | None = None):
        merged = dict(details or {})
        merged.setdefault("reason", reason)
        super().__init__(message, details=merged)
        self.reason = reason


# ---------------------------------------------------------------------------
# 状态冲突
# ---------------------------------------------------------------------------
class StateConflictError(KernelError):
    category = ErrorCategory.STATE_CONFLICT
    code = "E_STATE_CONFLICT"
    retryable = False


class RedirectLoopError(StateConflictError):
    code = "E_REDIRECT_LOOP"


class PolicySnapshotMismatchError(StateConflictError):
    code = "E_POLICY_SNAPSHOT_MISMATCH"


class AuditChainBrokenError(StateConflictError):
    code = "E_AUDIT_CHAIN_BROKEN"


class PinMismatchError(StateConflictError):
    """连接前复核发现对端地址不在已批准 pin 集合（纵深防御）。"""

    code = "E_PIN_MISMATCH"


# ---------------------------------------------------------------------------
# 资源耗尽
# ---------------------------------------------------------------------------
class ResourceExhaustedError(KernelError):
    category = ErrorCategory.RESOURCE_EXHAUSTED
    code = "E_RESOURCE_EXHAUSTED"
    retryable = True  # 换一个更大的预算/稍后重试可能成功


class RedirectBudgetError(ResourceExhaustedError):
    code = "E_REDIRECT_BUDGET"
    retryable = False


class DnsAnswerBudgetError(ResourceExhaustedError):
    code = "E_DNS_ANSWER_BUDGET"
    retryable = False


class TimeBudgetError(ResourceExhaustedError):
    code = "E_TIME_BUDGET"


# ---------------------------------------------------------------------------
# 计算失败
# ---------------------------------------------------------------------------
class ComputationFailedError(KernelError):
    category = ErrorCategory.COMPUTATION_FAILED
    code = "E_COMPUTATION_FAILED"
    retryable = True


class DnsResolutionError(ComputationFailedError):
    code = "E_DNS_UNKNOWN_NAME"


class ConnectError(ComputationFailedError):
    code = "E_CONNECT_FAILED"


class HttpProtocolError(ComputationFailedError):
    code = "E_HTTP_PROTOCOL"


class AuditSigningError(ComputationFailedError):
    code = "E_AUDIT_SIGN"
