"""验证失败分类。

四大顶层类别（任务要求彼此可区分）：
  input     输入错误：结构/编码不合法
  state     状态冲突：链状态与交易冲突
  resource  资源耗尽：栈元素/步数/深度等预算超限
  compute   计算失败：脚本执行期失败（下溢、验签失败、阈值不足等）

所有失败路径都返回 VerificationFailure，绝不执行任何转账状态变更。
"""

from __future__ import annotations

from dataclasses import dataclass

# 顶层类别
INPUT = "input"
STATE = "state"
RESOURCE = "resource"
COMPUTE = "compute"


@dataclass(frozen=True)
class FailureCode:
    code: str
    category: str
    message: str


# ---- 输入错误 input -------------------------------------------------------
MALFORMED_TX = FailureCode("input.malformed_tx", INPUT, "交易结构不合法")
MALFORMED_SCRIPT = FailureCode("input.malformed_script", INPUT, "脚本编码不合法")
MALFORMED_PUSH = FailureCode("input.malformed_push", INPUT, "变长推送长度与实际数据不符")
MALFORMED_PUBKEY = FailureCode("input.crypto.pubkey", INPUT, "公钥不是合法的 SEC1 secp256k1 编码")
MALFORMED_SIGNATURE = FailureCode("input.crypto.sig_encoding", INPUT, "签名不是合法 DER 编码")
MALFORMED_ENCODING = FailureCode("input.malformed_encoding", INPUT, "字段编码不合法")
UNKNOWN_OPCODE = FailureCode("input.unknown_opcode", INPUT, "操作码不在唯一支持列表中")
RESERVED_OPCODE = FailureCode("input.reserved_opcode", INPUT, "操作码被保留且禁止执行")
SCRIPT_TOO_LARGE = FailureCode("input.script_too_large", INPUT, "脚本原始字节超过上限")
DOMAIN_MISSING = FailureCode("input.domain_missing", INPUT, "交易缺少域标签")

# ---- 状态冲突 state -------------------------------------------------------
UNKNOWN_OUTPOINT = FailureCode("state.unknown_outpoint", STATE, "引用的 UTXO 不存在")
ALREADY_SPENT = FailureCode("state.already_spent", STATE, "UTXO 已被花费（双花）")
DOMAIN_CONFLICT = FailureCode("state.domain_conflict", STATE, "交易域标签与 UTXO 所在域不一致")
IMBALANCE = FailureCode("state.imbalance", STATE, "输入金额之和不等于输出金额之和")
BOOTSTRAP_CONFLICT = FailureCode("state.bootstrap_conflict", STATE, "创世纪状态已初始化且内容冲突")

# ---- 资源耗尽 resource ----------------------------------------------------
ELEMENT_TOO_LARGE = FailureCode("resource.element_too_large", RESOURCE, "栈元素超过字节上限")
STACK_OVERFLOW = FailureCode("resource.stack_overflow", RESOURCE, "栈元素个数超过上限")
OP_BUDGET_EXHAUSTED = FailureCode("resource.op_budget_exhausted", RESOURCE, "操作步数预算耗尽")
SCRIPT_DEPTH_EXCEEDED = FailureCode("resource.script_depth_exceeded", RESOURCE, "脚本嵌套分支深度超过上限")

# ---- 计算失败 compute -----------------------------------------------------
STACK_UNDERFLOW = FailureCode("compute.stack_underflow", COMPUTE, "栈元素不足（栈下溢）")
DISABLED_OPCODE = FailureCode("compute.disabled_opcode", COMPUTE, "操作码在受限脚本中被禁用")
UNBALANCED_IF = FailureCode("compute.unbalanced_if", COMPUTE, "IF/ELSE/ENDIF 不配对")
VERIFY_FAILED = FailureCode("compute.verify", COMPUTE, "VERIFY 校验的栈顶值为假")
RETURN_HIT = FailureCode("compute.op_return", COMPUTE, "执行到 OP_RETURN")
EQUALITY_FAILED = FailureCode("compute.equalverify", COMPUTE, "OP_EQUALVERIFY 两元素不相等")
CHECKSIG_FAILED = FailureCode("compute.crypto.sig", COMPUTE, "验签失败：签名与交易摘要/公钥不匹配（含错误交易域）")
THRESHOLD_NOT_MET = FailureCode("compute.crypto.threshold", COMPUTE, "CHECKMULTISIG：有效签名数未达到 m-of-n 门槛")
MULTISIG_POLICY = FailureCode("compute.crypto.threshold_invalid", COMPUTE, "m/n 非法或重复公钥")
SCRIPT_FINAL_FALSE = FailureCode("compute.script_false", COMPUTE, "脚本执行结束但栈顶为假")
SCRIPT_FINAL_EMPTY = FailureCode("compute.script_empty", COMPUTE, "脚本执行结束但栈为空")
DIRTY_STACK = FailureCode("compute.dirty_stack", COMPUTE, "clean-stack：执行结束后栈上残留多于一个元素")
INTERNAL_ERROR = FailureCode("compute.internal", COMPUTE, "验签计算内部异常")


class VerificationFailure(Exception):
    """携带分类码的验证失败。本身是异常，便于在执行栈中直接中断。

    注意：不能用 @dataclass(frozen=True) —— CPython 抛异常时需要写
    __traceback__ 属性。
    """

    code: FailureCode
    detail: str = ""
    pc: int | None = None

    def __init__(self, code: FailureCode, detail: str = "", pc: int | None = None):
        super().__init__(code.code)
        self.code = code
        self.detail = detail
        self.pc = pc

    def __str__(self) -> str:  # pragma: no cover - 调试用
        at = f" @pc={self.pc}" if self.pc is not None else ""
        return f"[{self.code.category}/{self.code.code}]{at} {self.code.message}: {self.detail}"

    def to_dict(self) -> dict:
        return {
            "category": self.code.category,
            "code": self.code.code,
            "message": self.code.message,
            "detail": self.detail,
            "pc": self.pc,
        }
