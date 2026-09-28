"""核心领域模型与错误分类。

错误码（``ErrorCode``）是跨 API / 存储 / 回放层共享的稳定契约：
独立测试按具体错误类别断言，而不是只检查 HTTP 200。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .. import __version__


class TxStatus(str, enum.Enum):
    """交易在池内/链上的生命周期状态。"""

    PENDING = "pending"        # 属于可执行连续前缀
    QUEUED = "queued"          # 在 nonce 缺口之后等待
    PROPOSED = "proposed"      # 已进入候选/提议区块，等待确认
    CONFIRMED = "confirmed"    # 已被确认深度最终确定
    ROLLED_BACK = "rolled_back"  # 区块回滚后离开链上状态（随后重新分类或重入池）
    DROPPED = "dropped"        # 过期、淘汰或被替换，终态保留行用于审计


class DropReason(str, enum.Enum):
    REPLACED = "replaced"              # 满足涨价条件的 RBF 替换
    EXPIRED = "expired"                # pending 超过 TTL
    EVICTED_CAPACITY = "evicted_capacity"      # 全局容量淘汰
    EVICTED_SENDER_LIMIT = "evicted_sender_limit"  # 单账户槽位淘汰
    ADMISSION_REJECTED = "admission_rejected"  # 入口拒绝（此时交易不会变成有效池内交易）
    REORG_INVALID = "reorg_invalid"    # 回滚后重入池校验失败


class ErrorCode(str, enum.Enum):
    """入口/操作失败的具体类别。"""

    INVALID_SIGNATURE = "invalid_signature"
    WRONG_CHAIN_ID = "wrong_chain_id"
    MALFORMED_TRANSACTION = "malformed_transaction"
    INTRINSIC_GAS_TOO_LOW = "intrinsic_gas_too_low"
    GAS_LIMIT_EXCEEDS_BLOCK = "gas_limit_exceeds_block"
    DATA_TOO_LARGE = "data_too_large"
    GAS_PRICE_BELOW_MINIMUM = "gas_price_below_minimum"
    NONCE_TOO_LOW = "nonce_too_low"              # nonce < 账户当前 nonce
    NONCE_TOO_FAR_AHEAD = "nonce_too_far_ahead"  # 超出 queued 跨度
    SENDER_SLOT_LIMIT = "sender_slot_limit"      # 单账户有效交易过多
    INSUFFICIENT_FUNDS = "insufficient_funds"
    SAME_NONCE_LOWER_PRICE = "same_nonce_lower_price"  # RBF 涨价不达标
    SAME_TRANSACTION_KNOWN = "same_transaction_known"  # 同哈希重复提交
    POOL_FULL = "pool_full"                      # 淘汰后仍无法容纳
    TX_NOT_FOUND = "tx_not_found"
    BLOCK_FULL = "block_full"                    # 候选区块一条也装不下
    BLOCK_ROLLBACK_FINALIZED = "block_rollback_finalized"
    BLOCK_NOT_FOUND = "block_not_found"
    CONFLICT = "conflict"


@dataclass
class TxError(Exception):
    """带稳定错误码的领域异常。

    作为异常可直接在服务层 raise；``details`` 中的字段会进入结构化日志与
    HTTP 错误体的 ``details``，便于复核。注意：异常类不可 frozen
    （Python 需要写入 ``__traceback__``）。
    """

    code: ErrorCode
    message: str
    details: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.code.value}: {self.message}"


@dataclass(frozen=True)
class Transaction:
    """已验签交易（不可变值对象）。

    费用模型采用简化的 EIP-1559 之前模型：单一 ``gas_price``（wei/gas），
    执行时按 ``gas_limit`` 预扣，确认时退还剩余。``data`` 仅承载合成负载，
    每字节贡献固定内在 gas。
    """

    tx_hash: str           # 0x + 64 hex，签名交易的 keccak256
    sender: str            # 0x + 40 hex，EIP-55 校验地址
    nonce: int
    gas_price: int
    gas_limit: int
    to: str                # 0x + 40 hex 合成接收者（不做存在性校验）
    value: int
    data: bytes
    v: int                 # EIP-155 v = chain_id*2 + 35/36
    r: int
    s: int
    raw: bytes             # RLP 编码的签名交易（存储与回放依据）


@dataclass(frozen=True)
class StoredTransaction:
    """交易 + 存储层元数据。"""

    tx: Transaction
    status: TxStatus
    received_at_ms: int
    status_reason: str = ""  # 最近一次移入该状态的理由码


@dataclass
class AccountState:
    """链状态内核中的账户视图。"""

    address: str
    balance: int
    nonce: int               # 下一个期望 nonce
    projected_balance: int   # 余额减去 pending/proposed 已承诺花费

    def committed_cost(self) -> int:
        return self.balance - self.projected_balance


@dataclass(frozen=True)
class Block:
    block_hash: str
    number: int
    parent_hash: str
    proposed_at_ms: int
    executed_tx_hashes: tuple[str, ...]   # 实际执行（扣费/nonce 前进）的交易
    skipped_tx_hashes: tuple[str, ...]    # 提议后执行期失败、未应用的交易
    gas_used: int


@dataclass(frozen=True)
class AuditEvent:
    """审计事件：每一次索引/状态迁移都对应一行可解释记录。"""

    audit_id: int | None
    request_id: str          # 关联触发该事件的请求身份
    occurred_at_ms: int
    event_type: str         # admitted / replaced / status_change / proposed / ...
    tx_hash: str | None
    block_hash: str | None
    reason: str             # 移入/移出理由，失败时为 ErrorCode 值
    detail: dict[str, object] = field(default_factory=dict)
    module: str = "local_txpool"
    service_version: str = __version__
