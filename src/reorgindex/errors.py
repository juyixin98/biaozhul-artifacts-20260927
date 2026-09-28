"""错误分类。每种失败都有稳定的 category，测试与 API 按类别断言。"""


class IndexError(Exception):
    """所有领域错误的基类。category 供 API/测试/诊断精确区分。"""

    category = "index_error"

    def __init__(self, message: str, **context):
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict:
        return {"category": self.category, "message": self.message, "context": self.context}


class DecodeError(IndexError):
    """区块/交易结构无法解码或字段不合法。"""

    category = "decode_error"


class VerificationError(IndexError):
    """哈希不匹配、默克尔根不一致或签名验证失败。"""

    category = "verification_error"


class UnknownParentError(IndexError):
    """父区块未知：进入悬挂池等待，不算拒绝。"""

    category = "unknown_parent"


class DuplicateBlockError(IndexError):
    category = "duplicate_block"


class FinalityReorgError(IndexError):
    """切换需要撤回已最终确定的区块，按模型必须拒绝。"""

    category = "finality_reorg"


class ConsensusRuleError(IndexError):
    """违反固定权重/结构规则（如工作量非正、高度不连续）。"""

    category = "consensus_rule"
