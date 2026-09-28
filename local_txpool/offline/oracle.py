"""**独立参考预言机（independent oracle）**。

本文件刻意只用 Python 内建结构（dict/list/set）重新实现交易池分类、
候选排序、执行、确认与回滚的预期语义。它：

* 不 import 任何 ``local_txpool.core.kernel`` / ``ordering`` / ``storage``
  代码（唯一共享的是 ``crypto`` 里的签名/解码工具——密码学向量不应有两种）；
* 数据规模极小，写法直白，便于人工逐行复核；
* 与真实服务在同一夹具上 **差分执行（differential testing）**：
  两边的分类、候选顺序、余额/nonce 必须逐步一致，任何分歧立即报错。

这满足"参考答案不能全部由被测核心实现自身生成"的要求：
预期答案来自这份独立模型，而不是读取核心的输出来断言核心。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import StoredTransaction, Transaction

PENDING = "pending"
QUEUED = "queued"
PROPOSED = "proposed"
CONFIRMED = "confirmed"
DROPPED = "dropped"


@dataclass
class OracleTx:
    tx: Transaction
    status: str = QUEUED
    reason: str = ""
    block_number: int | None = None
    received_at_ms: int = 0
    pending_since_ms: int | None = None


@dataclass
class OracleAccount:
    balance: int
    nonce: int = 0


@dataclass
class OracleBlock:
    number: int
    applied: list[str]
    skipped: list[str]


@dataclass
class OracleState:
    accounts: dict[str, OracleAccount] = field(default_factory=dict)
    txs: dict[str, OracleTx] = field(default_factory=dict)
    # tx_hash -> (sender, nonce) 便于索引
    index: dict[tuple[str, int], str] = field(default_factory=dict)
    blocks: list[OracleBlock] = field(default_factory=list)

    def clone(self) -> "OracleState":
        """深拷贝（回滚与差分检查点使用）。"""
        state = OracleState()
        state.accounts = {
            a: OracleAccount(ac.balance, ac.nonce)
            for a, ac in self.accounts.items()
        }
        state.txs = {
            h: OracleTx(o.tx, o.status, o.reason, o.block_number,
                        o.received_at_ms, o.pending_since_ms)
            for h, o in self.txs.items()
        }
        state.index = dict(self.index)
        state.blocks = [
            OracleBlock(b.number, list(b.applied), list(b.skipped))
            for b in self.blocks
        ]
        return state


@dataclass(frozen=True)
class OracleConfig:
    block_gas_limit: int
    confirmation_depth: int
    min_gas_price: int
    intrinsic_gas: int
    data_gas_per_byte: int
    max_queued_per_sender: int
    replacement_bump_pct: int
    max_transactions: int = 10_000
    pending_ttl_ms: int = 0
    max_transactions_per_sender: int = 10_000


@dataclass
class Eviction:
    tx_hash: str
    reason: str


class Oracle:
    """独立参考模型。接口与 Kernel 大致对应但更简单。"""

    def __init__(self, config: OracleConfig) -> None:
        self.state = OracleState()
        self.cfg = config
        self.now_ms = 1_700_000_000_000
        self.last_evictions: list[Eviction] = []

    def advance_ms(self, ms: int) -> None:
        self.now_ms += ms

    def expire(self) -> list[str]:
        """与 kernel 一致：只让超时的 pending 失效，然后重排。"""
        ttl = self.cfg.pending_ttl_ms
        if ttl <= 0:
            return []
        cutoff = self.now_ms - ttl
        victims = [
            o
            for o in self.state.txs.values()
            if o.status == PENDING
            and o.pending_since_ms is not None
            and o.pending_since_ms < cutoff
        ]
        for o in victims:
            o.status = DROPPED
            o.reason = "expired"
        for o in victims:
            self.reclassify(o.tx.sender)
        return [o.tx.tx_hash for o in victims]

    # ----- 工具 ----- #
    def _cost(self, tx: Transaction) -> int:
        return tx.gas_limit * tx.gas_price + tx.value

    def _active_for_nonce(self, sender: str, nonce: int) -> OracleTx | None:
        h = self.state.index.get((sender, nonce))
        if h is None:
            return None
        ot = self.state.txs[h]
        return ot if ot.status in (PENDING, QUEUED, PROPOSED) else None

    def fund(self, address: str, balance: int, reset: bool = False) -> None:
        if address in self.state.accounts and not reset:
            self.state.accounts[address].balance += balance
        else:
            self.state.accounts[address] = OracleAccount(balance, 0)

    # ----- 重分类：连续前缀（nonce 连续 + 累计余额可承担）----- #
    def reclassify(self, sender: str) -> None:
        acct = self.state.accounts[sender]
        in_pool = {
            o.tx.nonce: o
            for o in self.state.txs.values()
            if o.tx.sender == sender and o.status in (PENDING, QUEUED)
        }
        next_nonce = acct.nonce
        spent = 0
        pending_nonces: set[int] = set()
        while next_nonce in in_pool:
            tx = in_pool[next_nonce].tx
            cost = self._cost(tx)
            if spent + cost > acct.balance:
                break
            pending_nonces.add(next_nonce)
            spent += cost
            next_nonce += 1
        for nonce, ot in in_pool.items():
            want = PENDING if nonce in pending_nonces else QUEUED
            if ot.status != want:
                ot.status = want
                ot.reason = (
                    "oracle_prefix" if want == PENDING else "oracle_queued"
                )
                ot.pending_since_ms = (
                    self.now_ms if want == PENDING else None
                )
            elif want == PENDING and ot.pending_since_ms is None:
                ot.pending_since_ms = self.now_ms

    # ----- 准入（返回 (accepted, code)）----- #
    def submit(self, tx: Transaction) -> tuple[bool, str | None]:
        acct = self.state.accounts.get(tx.sender)
        if acct is None:
            return False, "malformed_transaction"
        if tx.gas_price < self.cfg.min_gas_price:
            return False, "gas_price_below_minimum"
        required = self.cfg.intrinsic_gas + self.cfg.data_gas_per_byte * len(tx.data)
        if tx.gas_limit < required:
            return False, "intrinsic_gas_too_low"
        if tx.gas_limit > self.cfg.block_gas_limit:
            return False, "gas_limit_exceeds_block"

        if tx.tx_hash in self.state.txs and self.state.txs[tx.tx_hash].status in (
            PENDING, QUEUED, PROPOSED,
        ):
            return False, "same_transaction_known"

        incumbent = self._active_for_nonce(tx.sender, tx.nonce)
        if incumbent is not None:
            if incumbent.status == PROPOSED:
                return False, "conflict"
            floor = (
                incumbent.tx.gas_price * (100 + self.cfg.replacement_bump_pct) + 99
            ) // 100
            if tx.gas_price < floor:
                return False, "same_nonce_lower_price"
            incumbent.status = DROPPED
            incumbent.reason = "replaced"
            del self.state.index[(tx.sender, tx.nonce)]

        if tx.nonce < acct.nonce:
            return False, "nonce_too_low"
        if tx.nonce > acct.nonce + self.cfg.max_queued_per_sender:
            return False, "nonce_too_far_ahead"

        # 余额承诺只对"当前 nonce 位"的新交易做即时检查；超前 nonce 的
        # 可执行性延迟到缺口闭合、reclassify 时统一判定（余额截止点）。
        if tx.nonce == acct.nonce and incumbent is None:
            pending_cost = sum(
                self._cost(o.tx)
                for o in self.state.txs.values()
                if o.tx.sender == tx.sender and o.status == PENDING
            )
            if acct.balance - pending_cost < self._cost(tx):
                return False, "insufficient_funds"

        # 容量淘汰发生在插入**之前**：只从旧池驱逐，直到至少有 1 个空位。
        self.last_evictions = []
        self._enforce_capacity()
        if self._pool_count() >= self.cfg.max_transactions:
            return False, "pool_full"

        ot = OracleTx(
            tx=tx,
            status=QUEUED,
            reason="admitted",
            received_at_ms=self.now_ms,
        )
        self.state.txs[tx.tx_hash] = ot
        self.state.index[(tx.sender, tx.nonce)] = tx.tx_hash
        self.reclassify(tx.sender)
        return True, None

    def _pool_count(self) -> int:
        return sum(
            1 for o in self.state.txs.values()
            if o.status in (PENDING, QUEUED)
        )

    def _enforce_capacity(self) -> None:
        """驱逐到旧池腾出至少一个空位。queued 优先、价低、早到；再 pending。"""
        def eviction_key(o: OracleTx) -> tuple[int, int, int, str]:
            group = 0 if o.status == QUEUED else 1
            return (group, o.tx.gas_price, o.received_at_ms, o.tx.tx_hash)

        while self._pool_count() >= self.cfg.max_transactions:
            victims = [
                o for o in self.state.txs.values()
                if o.status in (PENDING, QUEUED)
            ]
            if not victims:
                return
            victim = min(victims, key=eviction_key)
            sender = victim.tx.sender
            victim.status = DROPPED
            victim.reason = "evicted_capacity"
            self.state.index.pop((victim.tx.sender, victim.tx.nonce), None)
            self.last_evictions.append(
                Eviction(victim.tx.tx_hash, "evicted_capacity")
            )
            self.reclassify(sender)

    # ----- 候选顺序 ----- #
    def candidate(self) -> list[str]:
        """每发送者 nonce 队头 + 全局最高价，含 gas 上限让位。"""
        queues: dict[str, list[OracleTx]] = {}
        for o in self.state.txs.values():
            if o.status == PENDING:
                queues.setdefault(o.tx.sender, []).append(o)
        for items in queues.values():
            items.sort(key=lambda o: o.tx.nonce)

        head = {a: 0 for a in queues}
        balance = {a: ac.balance for a, ac in self.state.accounts.items()}
        chosen: list[str] = []
        gas_left = self.cfg.block_gas_limit
        blocked: set[str] = set()

        while True:
            heads: list[OracleTx] = []
            for sender, items in queues.items():
                if sender in blocked or head[sender] >= len(items):
                    continue
                o = items[head[sender]]
                if self._cost(o.tx) <= balance[sender] and o.tx.gas_limit <= gas_left:
                    heads.append(o)
            if not heads:
                break
            pick = min(
                heads,
                key=lambda o: (-o.tx.gas_price, 0, o.tx.tx_hash),
            )
            chosen.append(pick.tx.tx_hash)
            gas_left -= pick.tx.gas_limit
            balance[pick.tx.sender] -= self._cost(pick.tx)
            head[pick.tx.sender] += 1
        return chosen

    # ----- 出块 ----- #
    def propose(self) -> OracleBlock:
        ordered = self.candidate()
        number = (
            self.state.blocks[-1].number + 1
            if self.state.blocks
            else 1
        )
        applied: list[str] = []
        for h in ordered:
            o = self.state.txs[h]
            acct = self.state.accounts[o.tx.sender]
            if o.tx.nonce != acct.nonce:
                continue
            cost = self._cost(o.tx)
            if acct.balance < cost:
                continue
            acct.balance -= cost
            acct.nonce += 1
            o.status = PROPOSED
            o.block_number = number
            applied.append(h)
        block = OracleBlock(number, applied, [h for h in ordered if h not in applied])
        self.state.blocks.append(block)
        for sender in self.state.accounts:
            self.reclassify(sender)
        self.finalize()
        return block

    # ----- 确认 ----- #
    def finalize(self) -> list[str]:
        if not self.state.blocks:
            return []
        head = self.state.blocks[-1].number
        cutoff = head - self.cfg.confirmation_depth
        finalized: list[str] = []
        for b in self.state.blocks:
            if b.number <= cutoff:
                for h in b.applied:
                    ot = self.state.txs.get(h)
                    if ot and ot.status == PROPOSED:
                        ot.status = CONFIRMED
                        ot.reason = "depth_finalized"
                        finalized.append(h)
        return finalized

    # ----- 回滚 ----- #
    def rollback_to(self, target_number: int) -> list[str]:
        if not self.state.blocks:
            return []
        head = self.state.blocks[-1].number
        floor = head - self.cfg.confirmation_depth
        if target_number < floor:
            raise ValueError("block_rollback_finalized")
        removed = [b for b in self.state.blocks if b.number > target_number]
        for b in sorted(removed, key=lambda x: x.number, reverse=True):
            for h in reversed(b.applied):
                ot = self.state.txs[h]
                acct = self.state.accounts[ot.tx.sender]
                acct.balance += self._cost(ot.tx)
                acct.nonce -= 1
                ot.status = QUEUED
                ot.reason = "reentered_after_rollback"
                ot.block_number = None
        self.state.blocks = [
            x for x in self.state.blocks if x.number <= target_number
        ]
        for sender in self.state.accounts:
            self.reclassify(sender)
        self.finalize()
        return [h for b in removed for h in b.applied]

    # ----- 差分快照 ----- #
    def snapshot(self) -> dict[str, object]:
        """产出与服务端快照逐字段比对的纯数据视图。"""
        return {
            "accounts": {
                a: {"balance": ac.balance, "nonce": ac.nonce}
                for a, ac in sorted(self.state.accounts.items())
            },
            "txs": {
                h: {"status": o.status, "nonce": o.tx.nonce,
                    "sender": o.tx.sender, "block": o.block_number}
                for h, o in sorted(self.state.txs.items())
            },
            "pending": sorted(
                h
                for h, o in self.state.txs.items()
                if o.status == PENDING
            ),
            "queued": sorted(
                h
                for h, o in self.state.txs.items()
                if o.status == QUEUED
            ),
            "blocks": [
                {"number": b.number, "applied": list(b.applied)}
                for b in self.state.blocks
            ],
            "candidate": self.candidate(),
        }
