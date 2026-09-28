"""错误分类契约 (error taxonomy contract).

四类失败在整条流水线（解码 -> 校验 -> 状态应用 -> 回放）中使用同一组分类，
调用方可凭 ``ErrorCategory`` 做粗粒度判定，凭 ``code`` 做细粒度判定。

    INPUT_ERROR          输入本身非法（与状态无关），调用方修改输入后可重试。
    STATE_CONFLICT       输入本身可能合法，但与已提交链状态/块内暂定状态冲突。
    RESOURCE_EXHAUSTED   输入触及预设的资源上限（大小/条数/容量）。
    COMPUTATION_FAILED   执行确定检查时失败：签名验证、编码/摘要不一致、底层异常。

每个错误都带：
    code        稳定的机器可读细类字符串（如 ``DOUBLE_SPEND``）
    message     人类可读说明
    details     机器可读上下文（如 outpoint、tx_index、limit）
    tx_index    块内定位（适用时）；整块级错误为 None

所有 LedgerError 都会在 :mod:`utxo_ledger.api` 中映射为 4xx/5xx JSON 信封。
"""
from __future__ import annotations

import enum
from typing import Any


class ErrorCategory(str, enum.Enum):
    """粗粒度错误类别。字符串值固定，写入日志/JSON，禁止改名。"""

    INPUT_ERROR = "INPUT_ERROR"
    STATE_CONFLICT = "STATE_CONFLICT"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    COMPUTATION_FAILED = "COMPUTATION_FAILED"


class LedgerError(Exception):
    """所有账本错误的基类。子类必须设置 ``category`` 与 ``code``。"""

    category: ErrorCategory = ErrorCategory.INPUT_ERROR
    code: str = "LEDGER_ERROR"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        tx_index: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})
        self.tx_index = tx_index

    def to_dict(self) -> dict[str, Any]:
        """转换为稳定的 JSON 可序列化结构（API 错误信封与日志共用）。"""
        out: dict[str, Any] = {
            "category": self.category.value,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }
        if self.tx_index is not None:
            out["tx_index"] = self.tx_index
        return out


# ---------------------------------------------------------------------------
# 1) INPUT_ERROR —— 与链状态无关的输入缺陷
# ---------------------------------------------------------------------------
class MalformedEncodingError(LedgerError):
    """二进制/JSON 编码不合法：截断、长度不符、未知键、类型不符、版本错误。"""

    category = ErrorCategory.INPUT_ERROR
    code = "MALFORMED_ENCODING"


class ZeroValueError(LedgerError):
    """输出金额为 0（零值输出被明确禁止）。"""

    category = ErrorCategory.INPUT_ERROR
    code = "ZERO_VALUE"


class AmountOutOfRangeError(LedgerError):
    """金额为负数或超过 MAX_MONEY（2^63-1），或费用为负。"""

    category = ErrorCategory.INPUT_ERROR
    code = "AMOUNT_OUT_OF_RANGE"


class AmountOverflowError(LedgerError):
    """求和超过 u64/有界整数安全上限（加法溢出保护触发）。"""

    category = ErrorCategory.INPUT_ERROR
    code = "AMOUNT_OVERFLOW"


class WitnessCountMismatchError(LedgerError):
    """见证数量与输入数量不一致，或同一输入给出多条见证。"""

    category = ErrorCategory.INPUT_ERROR
    code = "WITNESS_COUNT_MISMATCH"


class TxidDuplicateError(LedgerError):
    """同一区块内出现两个相同 txid。"""

    category = ErrorCategory.INPUT_ERROR
    code = "TXID_DUPLICATE"


class IllegalIssuanceError(LedgerError):
    """非 genesis 交易使用 issue（无输入铸造）；或 genesis 交易带输入/费用。"""

    category = ErrorCategory.INPUT_ERROR
    code = "ILLEGAL_ISSUE"


class InvalidFeeError(LedgerError):
    """费用为负数（费用 > 输入总额在守恒检查中单独报）。"""

    category = ErrorCategory.INPUT_ERROR
    code = "INVALID_FEE"


class ConservationMismatchError(LedgerError):
    """输入总额 != 输出总额 + 费用（价值不守恒）。"""

    category = ErrorCategory.INPUT_ERROR
    code = "CONSERVATION_MISMATCH"


class BadGenesisError(LedgerError):
    """genesis 块结构非法（高度不为 0、含费用、tip 已存在却再提交 genesis 等）。"""

    category = ErrorCategory.INPUT_ERROR
    code = "BAD_GENESIS"


# ---------------------------------------------------------------------------
# 2) STATE_CONFLICT —— 与已提交状态或块内暂定状态冲突
# ---------------------------------------------------------------------------
class DoubleSpendError(LedgerError):
    """引用的 outpoint 已被花费：已提交链上已花费，或块内重复输入/双花。

    块内同一输入重复出现也归此类别（"重复输入"）。
    """

    category = ErrorCategory.STATE_CONFLICT
    code = "DOUBLE_SPEND"


class UnknownOutpointError(LedgerError):
    """引用的 outpoint 在链上与块内均不存在。"""

    category = ErrorCategory.STATE_CONFLICT
    code = "UNKNOWN_OUTPOINT"


class ForwardReferenceError(LedgerError):
    """交易引用了块内排在其后（更大 tx_index）的交易。"""

    category = ErrorCategory.STATE_CONFLICT
    code = "FORWARD_REFERENCE"


class ReferenceCycleError(LedgerError):
    """块内交易依赖图成环。"""

    category = ErrorCategory.STATE_CONFLICT
    code = "REFERENCE_CYCLE"


class BlockConflictError(LedgerError):
    """块与链状态冲突：高度不连续、prev_hash 不符、重复块。"""

    category = ErrorCategory.STATE_CONFLICT
    code = "BLOCK_CONFLICT"


# ---------------------------------------------------------------------------
# 3) RESOURCE_EXHAUSTED —— 资源/预算上限
# ---------------------------------------------------------------------------
class ResourceLimitError(LedgerError):
    """触及 Limits 中的某一上限。details 含 limit_name、limit、value。"""

    category = ErrorCategory.RESOURCE_EXHAUSTED
    code = "RESOURCE_LIMIT"


# ---------------------------------------------------------------------------
# 4) COMPUTATION_FAILED —— 确定的计算/验证失败与底层故障
# ---------------------------------------------------------------------------
class SignatureError(LedgerError):
    """签名验证失败：签名被篡改、公钥与见证不匹配、DER 无法解析。"""

    category = ErrorCategory.COMPUTATION_FAILED
    code = "SIGNATURE_INVALID"


class HashMismatchError(LedgerError):
    """声明的 txid / block_id 与根据固定编码重新计算的结果不一致。"""

    category = ErrorCategory.COMPUTATION_FAILED
    code = "HASH_MISMATCH"


class MerkleRootMismatchError(LedgerError):
    """块头 tx_root / witness_root 与实际交易列表计算结果不符。"""

    category = ErrorCategory.COMPUTATION_FAILED
    code = "ROOT_MISMATCH"


class StorageFailureError(LedgerError):
    """索引存储层故障（sqlite 错误、I/O 错误等）。"""

    category = ErrorCategory.COMPUTATION_FAILED
    code = "STORAGE_FAILURE"


class InternalError(LedgerError):
    """未预期的计算故障兜底（不应发生；发生即实现缺陷）。"""

    category = ErrorCategory.COMPUTATION_FAILED
    code = "INTERNAL_ERROR"
