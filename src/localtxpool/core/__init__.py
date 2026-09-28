"""链内核的领域常量与记录类型。

状态机（status）
~~~~~~~~~~~~~~~~
``queued`` -> ``pending`` -> ``included`` -> ``mined``
任一活跃状态 -> ``expired`` / ``replaced`` / ``evicted``（离开活跃池，索引槽位释放）
``mined`` 在区块回滚后回到 ``pending``/``queued``/``expired``

每个 ``status`` 都带一个 ``reason`` 说明**为什么**处于该状态，
并由 journals 记录每次移入移出，使候选区块顺序与每个动作可解释。
"""

from __future__ import annotations

from dataclasses import dataclass

# ---- 交易状态 ---- #
PENDING = "pending"      # 位于某账户的可执行连续前缀
QUEUED = "queued"        # 因 nonce 缺口/余额不足而等待
INCLUDED = "included"    # 已进入某个 proposed 候选区块，等待确认
MINED = "mined"          # 区块已确认，交易上链
EXPIRED = "expired"      # 超过 ttl
REPLACED = "replaced"    # 被同 nonce 的更高价交易替换
EVICTED = "evicted"      # 容量淘汰

ACTIVE_STATUSES = (PENDING, QUEUED, INCLUDED)
POOL_STATUSES = (PENDING, QUEUED)  # 仍参与可执行前缀计算

# ---- 区块状态 ---- #
BLOCK_PROPOSED = "proposed"
BLOCK_CONFIRMED = "confirmed"

# ---- 状态理由码（reason）：每次分类/移入移出都必须给出 ---- #
REASON_EXECUTABLE = "EXECUTABLE"                       # 连续前缀且负担得起
REASON_NONCE_GAP = "NONCE_GAP"                         # 缺少更低 nonce
REASON_GAP_AFFORDABILITY = "GAP_AFFORDABILITY"         # 更低 nonce 交易余额不足，前缀在此断裂
REASON_INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"       # 本身余额不足（且没有更便宜的非缺口交易，用于首个非缺口位置）
REASON_INCLUDED = "INCLUDED_IN_BLOCK"                  # 被候选区块收录
REASON_CONFIRMED = "CONFIRMED"                         # 区块确认上链
REASON_EXPIRED_TTL = "EXPIRED_TTL"                     # TTL 到期
REASON_REPLACED_PRICE = "REPLACED_PRICE_BUMP"          # 被满足涨价条件的交易替换
REASON_EVICTED_QUEUE_ACCOUNT = "EVICTED_QUEUE_ACCOUNT" # 账户 queued 容量淘汰
REASON_EVICTED_QUEUE_GLOBAL = "EVICTED_QUEUE_GLOBAL"   # 全局 queued 容量淘汰
REASON_BLOCK_DISCARDED = "BLOCK_DISCARDED"             # 候选区块被丢弃，重回池中
REASON_ROLLBACK_REEXEC = "ROLLBACK_REEXEC"             # 区块回滚后重新分类

ALL_REASONS = {
    REASON_EXECUTABLE,
    REASON_NONCE_GAP,
    REASON_GAP_AFFORDABILITY,
    REASON_INSUFFICIENT_FUNDS,
    REASON_INCLUDED,
    REASON_CONFIRMED,
    REASON_EXPIRED_TTL,
    REASON_REPLACED_PRICE,
    REASON_EVICTED_QUEUE_ACCOUNT,
    REASON_EVICTED_QUEUE_GLOBAL,
    REASON_BLOCK_DISCARDED,
    REASON_ROLLBACK_REEXEC,
}


@dataclass(slots=True)
class TxRecord:
    """仓储层读出的一条交易记录（含当前池状态）。"""

    tx_hash: str
    raw: bytes
    sender: str
    to_addr: str | None
    nonce: int
    gas_price: int
    gas_limit: int
    value: int
    data: bytes
    chain_id: int
    received_at: int
    expires_at: int
    status: str
    reason: str
    reason_detail: str
    replaced_by: str | None
    block_number: int | None
    position: int | None
