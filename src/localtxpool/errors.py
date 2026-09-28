"""错误类型：每个失败类别都有稳定的机器可读 ``code``。

这些 code 会原样出现在 HTTP 错误响应、结构化日志和测试断言中；
独立测试按 code 断言失败类别，而不是只断言“调用失败”。
"""

from __future__ import annotations

# ---- HTTP 层使用的稳定错误码（也是测试断言依据） ----
BAD_REQUEST = "BAD_REQUEST"
INVALID_SIGNATURE = "INVALID_SIGNATURE"
WRONG_CHAIN_ID = "WRONG_CHAIN_ID"
INTRINSIC_GAS = "INTRINSIC_GAS"
UNDERPRICED = "UNDERPRICED"
INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
NONCE_TOO_LOW = "NONCE_TOO_LOW"
NONCE_TOO_FAR = "NONCE_TOO_FAR"
REPLACEMENT_UNDERPRICED = "REPLACEMENT_UNDERPRICED"
ALREADY_KNOWN = "ALREADY_KNOWN"
ACCOUNT_QUEUE_FULL = "ACCOUNT_QUEUE_FULL"
POOL_FULL = "POOL_FULL"
EXPIRED = "EXPIRED"
NOT_FOUND = "NOT_FOUND"
BLOCK_CONFLICT = "BLOCK_CONFLICT"
EMPTY_BLOCK = "EMPTY_BLOCK"
BLOCK_NOT_PROPOSED = "BLOCK_NOT_PROPOSED"
ROLLBACK_TOO_DEEP = "ROLLBACK_TOO_DEEP"
INVALID_STATE = "INVALID_STATE"


class PoolError(Exception):
    """交易池/链内核错误基类。"""

    code: str = INVALID_STATE
    http_status: int = 400

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": self.code, "message": str(self), "details": self.details}


class BadRequest(PoolError):
    code = BAD_REQUEST
    http_status = 400


class TxDecodeError(PoolError):
    """RLP 字节流无法严格解码为一条遗留交易。"""

    code = BAD_REQUEST
    http_status = 400


class InvalidSignature(PoolError):
    code = INVALID_SIGNATURE
    http_status = 400


class WrongChainId(PoolError):
    code = WRONG_CHAIN_ID
    http_status = 400


class IntrinsicGasTooLow(PoolError):
    code = INTRINSIC_GAS
    http_status = 400


class Underpriced(PoolError):
    code = UNDERPRICED
    http_status = 400


class InsufficientFunds(PoolError):
    code = INSUFFICIENT_FUNDS
    http_status = 400


class NonceTooLow(PoolError):
    code = NONCE_TOO_LOW
    http_status = 400


class NonceTooFar(PoolError):
    code = NONCE_TOO_FAR
    http_status = 400


class ReplacementUnderpriced(PoolError):
    code = REPLACEMENT_UNDERPRICED
    http_status = 409


class AlreadyKnown(PoolError):
    code = ALREADY_KNOWN
    http_status = 409


class AccountQueueFull(PoolError):
    code = ACCOUNT_QUEUE_FULL
    http_status = 429


class PoolFull(PoolError):
    code = POOL_FULL
    http_status = 429


class Expired(PoolError):
    code = EXPIRED
    http_status = 400


class NotFound(PoolError):
    code = NOT_FOUND
    http_status = 404


class BlockConflict(PoolError):
    code = BLOCK_CONFLICT
    http_status = 409


class EmptyBlock(PoolError):
    code = EMPTY_BLOCK
    http_status = 400


class BlockNotProposed(PoolError):
    code = BLOCK_NOT_PROPOSED
    http_status = 409


class RollbackTooDeep(PoolError):
    code = ROLLBACK_TOO_DEEP
    http_status = 400


class InvalidState(PoolError):
    code = INVALID_STATE
    http_status = 500
