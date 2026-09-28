"""失败分类。

四类失败必须可区分：
  INPUT    输入错误（请求/交易/脚本结构、未知操作码、元素过大）
  RESOURCE 资源耗尽（预算、栈、脚本长度、嵌套/上下文深度）
  COMPUTE  计算失败（栈下溢、求值为假、验签失败、重复签名、阈值不足、数值溢出等）
  STATE    状态冲突（UTXO 不存在、交易重复、日志/状态根不一致）
另有 SUCCESS。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FailKind(str, Enum):
    SUCCESS = "SUCCESS"
    INPUT = "INPUT"
    RESOURCE = "RESOURCE"
    COMPUTE = "COMPUTE"
    STATE = "STATE"


class FailCode(str, Enum):
    # ---- 成功 ----
    OK = "OK"

    # ---- 输入错误 INPUT ----
    REQUEST_MALFORMED = "REQUEST_MALFORMED"
    TX_MALFORMED = "TX_MALFORMED"
    SCRIPT_MALFORMED = "SCRIPT_MALFORMED"
    UNKNOWN_OPCODE = "UNKNOWN_OPCODE"
    PUSH_ONLY_VIOLATION = "PUSH_ONLY_VIOLATION"
    ELEMENT_TOO_LARGE = "ELEMENT_TOO_LARGE"

    # ---- 资源耗尽 RESOURCE ----
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    STACK_TOO_LARGE = "STACK_TOO_LARGE"
    SCRIPT_TOO_LARGE = "SCRIPT_TOO_LARGE"
    CONDITION_DEPTH_EXCEEDED = "CONDITION_DEPTH_EXCEEDED"
    SCRIPT_DEPTH_EXCEEDED = "SCRIPT_DEPTH_EXCEEDED"

    # ---- 计算失败 COMPUTE ----
    STACK_UNDERFLOW = "STACK_UNDERFLOW"
    EVAL_FALSE = "EVAL_FALSE"
    UNCLEAN_STACK = "UNCLEAN_STACK"
    SIG_INVALID = "SIG_INVALID"
    SIG_DUPLICATED = "SIG_DUPLICATED"
    THRESHOLD_NOT_MET = "THRESHOLD_NOT_MET"
    MULTISIG_MALFORMED = "MULTISIG_MALFORMED"
    INT_OVERFLOW = "INT_OVERFLOW"
    UNBALANCED_CONDITIONAL = "UNBALANCED_CONDITIONAL"
    VALUE_IMBALANCE = "VALUE_IMBALANCE"
    OP_RETURN_EXECUTED = "OP_RETURN_EXECUTED"

    # ---- 状态冲突 STATE ----
    UTXO_MISSING = "UTXO_MISSING"
    TX_ALREADY_ACCEPTED = "TX_ALREADY_ACCEPTED"
    JOURNAL_CORRUPT = "JOURNAL_CORRUPT"
    STATE_ROOT_MISMATCH = "STATE_ROOT_MISMATCH"


_KIND: dict[FailCode, FailKind] = {
    FailCode.OK: FailKind.SUCCESS,
    FailCode.REQUEST_MALFORMED: FailKind.INPUT,
    FailCode.TX_MALFORMED: FailKind.INPUT,
    FailCode.SCRIPT_MALFORMED: FailKind.INPUT,
    FailCode.UNKNOWN_OPCODE: FailKind.INPUT,
    FailCode.PUSH_ONLY_VIOLATION: FailKind.INPUT,
    FailCode.ELEMENT_TOO_LARGE: FailKind.INPUT,
    FailCode.BUDGET_EXHAUSTED: FailKind.RESOURCE,
    FailCode.STACK_TOO_LARGE: FailKind.RESOURCE,
    FailCode.SCRIPT_TOO_LARGE: FailKind.RESOURCE,
    FailCode.CONDITION_DEPTH_EXCEEDED: FailKind.RESOURCE,
    FailCode.SCRIPT_DEPTH_EXCEEDED: FailKind.RESOURCE,
    FailCode.STACK_UNDERFLOW: FailKind.COMPUTE,
    FailCode.EVAL_FALSE: FailKind.COMPUTE,
    FailCode.UNCLEAN_STACK: FailKind.COMPUTE,
    FailCode.SIG_INVALID: FailKind.COMPUTE,
    FailCode.SIG_DUPLICATED: FailKind.COMPUTE,
    FailCode.THRESHOLD_NOT_MET: FailKind.COMPUTE,
    FailCode.MULTISIG_MALFORMED: FailKind.COMPUTE,
    FailCode.INT_OVERFLOW: FailKind.COMPUTE,
    FailCode.UNBALANCED_CONDITIONAL: FailKind.COMPUTE,
    FailCode.VALUE_IMBALANCE: FailKind.COMPUTE,
    FailCode.OP_RETURN_EXECUTED: FailKind.COMPUTE,
    FailCode.UTXO_MISSING: FailKind.STATE,
    FailCode.TX_ALREADY_ACCEPTED: FailKind.STATE,
    FailCode.JOURNAL_CORRUPT: FailKind.STATE,
    FailCode.STATE_ROOT_MISMATCH: FailKind.STATE,
}

_DEFAULT_REASON: dict[FailCode, str] = {
    FailCode.OK: "验证通过",
    FailCode.REQUEST_MALFORMED: "请求体不符合交易结构",
    FailCode.TX_MALFORMED: "交易字段非法",
    FailCode.SCRIPT_MALFORMED: "脚本字节流结构非法",
    FailCode.UNKNOWN_OPCODE: "命中操作码白名单之外的字节",
    FailCode.PUSH_ONLY_VIOLATION: "解锁脚本包含非压入操作",
    FailCode.ELEMENT_TOO_LARGE: "栈元素超过大小上限",
    FailCode.BUDGET_EXHAUSTED: "操作步数预算耗尽",
    FailCode.STACK_TOO_LARGE: "栈元素个数超过上限",
    FailCode.SCRIPT_TOO_LARGE: "脚本长度超过上限",
    FailCode.CONDITION_DEPTH_EXCEEDED: "条件分支嵌套深度超过上限",
    FailCode.SCRIPT_DEPTH_EXCEEDED: "脚本上下文深度超过上限",
    FailCode.STACK_UNDERFLOW: "栈下溢：弹出时栈中没有足够元素",
    FailCode.EVAL_FALSE: "条件求值为假",
    FailCode.UNCLEAN_STACK: "脚本结束时栈不为恰好一个真元素",
    FailCode.SIG_INVALID: "签名无法绑定到交易摘要与公钥",
    FailCode.SIG_DUPLICATED: "同一公钥/签名被重复计数",
    FailCode.THRESHOLD_NOT_MET: "M-of-N 门槛未满足",
    FailCode.MULTISIG_MALFORMED: "M-of-N 参数非法",
    FailCode.INT_OVERFLOW: "ScriptNum 超过允许宽度/取值",
    FailCode.UNBALANCED_CONDITIONAL: "IF/ELSE/ENDIF 不平衡",
    FailCode.VALUE_IMBALANCE: "输入金额合计不等于输出金额合计",
    FailCode.OP_RETURN_EXECUTED: "活跃分支执行到 OP_RETURN",
    FailCode.UTXO_MISSING: "花费的 UTXO 不存在或已被花费",
    FailCode.TX_ALREADY_ACCEPTED: "交易已被接受，禁止重复入账",
    FailCode.JOURNAL_CORRUPT: "写前日志哈希链断裂",
    FailCode.STATE_ROOT_MISMATCH: "回放状态根与索引存储不一致",
}


def kind_of(code: FailCode) -> FailKind:
    return _KIND[code]


class VmFailure(Exception):
    """携带稳定失败分类的异常；永不携带敏感密钥信息。"""

    def __init__(self, code: FailCode, detail: str | None = None):
        self.code = code
        self.kind = kind_of(code)
        self.detail = detail or _DEFAULT_REASON[code]
        super().__init__(f"[{self.kind.value}/{code.value}] {self.detail}")

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "code": self.code.value, "detail": self.detail}


@dataclass(frozen=True)
class FailureResult:
    code: FailCode
    detail: str

    @property
    def kind(self) -> FailKind:
        return kind_of(self.code)

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "code": self.code.value, "detail": self.detail}


def failure_result(code: FailCode, detail: str | None = None) -> FailureResult:
    return FailureResult(code=code, detail=detail or _DEFAULT_REASON[code])
