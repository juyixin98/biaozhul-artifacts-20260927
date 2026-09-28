"""交易池内核：可执行前缀分类、同 nonce 替换、过期、容量淘汰、候选选择。

本模块只依赖抽象的 :class:`~localtxpool.storage.repository.Repository`，
不接触 HTTP、时钟与配置以外的任何外部设施。所有状态迁移都写 journal。

分类规则（费用再高也不能跳过自身 nonce 缺口）
----------------------------------------------
对每个发送者，从链上 nonce 起按 nonce 递增扫描其活跃交易：

1. 当前 nonce 没有交易          -> 后面全部 ``queued``（NONCE_GAP）。
2. 有交易但 ``balance < max_cost`` -> 前缀在此**断裂**：该交易以及该发送者
   其余全部 ``queued``（该交易 INSUFFICIENT_FUNDS，其余 GAP_AFFORDABILITY）。
3. 负担得起                      -> ``pending``，并沿 nonce **逐笔扣减预估花费**
   （``value + gas_limit*gas_price``）继续扫描——余额只够前缀中前几笔时，
   之后的交易落入 queued（INSUFFICIENT_FUNDS / GAP_AFFORDABILITY）。
   实际余额只在区块确认时真实扣减，这里只是保守的可执行性投影。
"""

from __future__ import annotations

import heapq

from ..clock import Clock
from ..config import PoolConfig
from ..encoding import SignedTransaction, to_checksum_address
from ..errors import (
    AccountQueueFull,
    AlreadyKnown,
    InsufficientFunds,
    NonceTooFar,
    NonceTooLow,
    PoolFull,
    ReplacementUnderpriced,
    Underpriced,
)
from . import (
    PENDING,
    QUEUED,
    REASON_EVICTED_QUEUE_ACCOUNT,
    REASON_EVICTED_QUEUE_GLOBAL,
    REASON_EXECUTABLE,
    REASON_EXPIRED_TTL,
    REASON_GAP_AFFORDABILITY,
    REASON_INSUFFICIENT_FUNDS,
    REASON_NONCE_GAP,
    REASON_REPLACED_PRICE,
    TxRecord,
)
from ..storage.repository import Repository, UniqueActiveViolation

# 最小 gas 价格（本地固定值；低于它直接拒绝）
MIN_GAS_PRICE = 1


class Mempool:
    def __init__(self, repo: Repository, config: PoolConfig, clock: Clock):
        self.repo = repo
        self.cfg = config
        self.clock = clock

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #
    def _journal(self, *, request_id: str, action: str, tx: TxRecord | None = None,
                 from_status: str, to_status: str, reason: str,
                 detail: dict | None = None, sender: str | None = None,
                 block_number: int | None = None) -> None:
        self.repo.add_journal(
            ts=self.clock.now(),
            request_id=request_id,
            action=action,
            tx_hash=tx.tx_hash if tx else None,
            sender=(tx.sender if tx else sender),
            block_number=block_number,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            detail=detail,
        )

    # ------------------------------------------------------------------ #
    # 可执行前缀分类
    # ------------------------------------------------------------------ #
    def classify_sender(self, sender: str, *, request_id: str = "",
                        reason: str = REASON_EXECUTABLE) -> dict:
        """对单个发送者重新分类全部 pending/queued 交易。

        返回 ``{"pending": [hash...], "queued": [(hash, reason)...]}``，
        供调用方（测试/回放）直接断言。
        """
        sender = sender.lower()
        now = self.clock.now()
        account = self.repo.get_account(sender)
        balance = int(account["balance"]) if account else 0
        onchain_nonce = account["nonce"] if account else 0

        txs = self.repo.list_sender(sender, (PENDING, QUEUED))
        by_nonce = {t.nonce: t for t in txs}
        result: dict = {"sender": to_checksum_address(sender), "balance": balance,
                        "onchain_nonce": onchain_nonce, "pending": [], "queued": []}

        # 1) 先做惰性过期判定（过期永远不参与前缀计算）
        live: dict[int, TxRecord] = {}
        for t in txs:
            if t.expires_at <= now:
                if t.status != "expired":
                    self.repo.set_tx_status(
                        t.tx_hash, "expired", REASON_EXPIRED_TTL,
                        f"expires_at={t.expires_at} <= now={now}", now)
                    self._journal(request_id=request_id, action="expire", tx=t,
                                  from_status=t.status, to_status="expired",
                                  reason=REASON_EXPIRED_TTL,
                                  detail={"expires_at": t.expires_at, "now": now})
                    result["queued"].append((t.tx_hash, REASON_EXPIRED_TTL))
                continue
            live[t.nonce] = t

        # 2) 从链上 nonce 开始连续扫描；projected 是“前面 pending 交易都执行后”
        #    剩余的预估余额，保证整条前缀同时可执行，而不是每笔各自独立判断。
        nonce = onchain_nonce
        projected = balance
        max_n = max(live, default=onchain_nonce - 1)
        while nonce <= max_n:
            t = live.get(nonce)
            if t is None:
                # 缺口：之后全部因 NONCE_GAP 等待
                for gap_nonce in range(nonce + 1, max_n + 1):
                    gt = live.get(gap_nonce)
                    if gt is not None:
                        self._move(gt, QUEUED, REASON_NONCE_GAP,
                                   f"missing preceding nonce {nonce}",
                                   request_id)
                        result["queued"].append((gt.tx_hash, REASON_NONCE_GAP))
                break
            cost = t.value + t.gas_limit * t.gas_price
            if projected < cost:
                # 余额断裂：该笔与后续全部等待
                self._move(t, QUEUED, REASON_INSUFFICIENT_FUNDS,
                           f"projected balance {projected} < max_cost {cost}",
                           request_id)
                result["queued"].append((t.tx_hash, REASON_INSUFFICIENT_FUNDS))
                for later_nonce in range(nonce + 1, max_n + 1):
                    lt = live.get(later_nonce)
                    if lt is not None:
                        self._move(lt, QUEUED, REASON_GAP_AFFORDABILITY,
                                   f"nonce {nonce} tx is unaffordable: prefix broken",
                                   request_id)
                        result["queued"].append((lt.tx_hash, REASON_GAP_AFFORDABILITY))
                break
            # 负担得起 -> pending，并沿链扣减预估花费
            self._move(t, PENDING, REASON_EXECUTABLE,
                       f"nonce {nonce} contiguous from on-chain nonce {onchain_nonce} "
                       f"and affordable (projected {projected} -> {projected - cost})",
                       request_id)
            result["pending"].append(t.tx_hash)
            projected -= cost
            nonce += 1

        return result

    def _move(self, tx: TxRecord, new_status: str, reason: str, detail: str,
              request_id: str) -> bool:
        """把交易迁到新状态；状态真的变化时写 journal。返回是否发生了迁移。"""
        if tx.status == new_status and tx.reason == reason:
            return False
        old = tx.status
        self.repo.set_tx_status(tx.tx_hash, new_status, reason, detail, self.clock.now())
        tx.status = new_status
        tx.reason = reason
        self._journal(request_id=request_id, action="classify", tx=tx,
                      from_status=old, to_status=new_status, reason=reason,
                      detail={"detail": detail})
        return True

    def _projected_balance(self, sender: str, balance: int, onchain_nonce: int,
                           before_nonce: int) -> int:
        """从链上 nonce 到 before_nonce（不含）之间，当前 pending 交易全部执行后
        的预估余额。用于判断替换/新交易是否仍让前缀可负担。"""
        projected = balance
        for t in self.repo.list_sender(sender, (PENDING,)):
            if onchain_nonce <= t.nonce < before_nonce:
                projected -= t.value + t.gas_limit * t.gas_price
        return projected

    # ------------------------------------------------------------------ #
    # 过期
    # ------------------------------------------------------------------ #
    def reap_expired(self, *, request_id: str = "") -> list[str]:
        """标记所有到期交易（含 included——候选区块中的交易也可能过期）。"""
        now = self.clock.now()
        expired_hashes: list[str] = []
        for t in self.repo.list_expirable(now):
            self.repo.set_tx_status(
                t.tx_hash, "expired", REASON_EXPIRED_TTL,
                f"expires_at={t.expires_at} <= now={now}", now)
            self._journal(request_id=request_id, action="expire", tx=t,
                          from_status=t.status, to_status="expired",
                          reason=REASON_EXPIRED_TTL,
                          detail={"expires_at": t.expires_at, "now": now})
            expired_hashes.append(t.tx_hash)
        return expired_hashes

    # ------------------------------------------------------------------ #
    # 容量淘汰（只淘汰 queued；pending/included 永不被挤掉）
    # ------------------------------------------------------------------ #
    def _evict_one_queued(self, *, exclude_sender: str | None, request_id: str,
                          reason: str, detail: str) -> TxRecord | None:
        victim = self.repo.cheapest_queued(exclude_sender=exclude_sender)
        if victim is None:
            return None
        now = self.clock.now()
        self.repo.set_tx_status(victim.tx_hash, "evicted", reason, detail, now)
        self._journal(request_id=request_id, action="evict", tx=victim,
                      from_status=victim.status, to_status="evicted",
                      reason=reason, detail={"detail": detail,
                                             "gas_price": victim.gas_price})
        return victim

    # ------------------------------------------------------------------ #
    # 接收新交易
    # ------------------------------------------------------------------ #
    def accept(self, tx: SignedTransaction, *, request_id: str = "") -> dict:
        """校验、替换判定、容量控制，然后分类。

        返回 {"status": pending|queued, "reason": ..., "tx_hash": ...}。
        调用方必须在数据库事务内调用本方法。
        """
        now = self.clock.now()
        sender = to_checksum_address(tx.from_address).lower()
        tx_hash = "0x" + tx.hash().hex()

        # 0) 重放：同哈希
        if self.repo.get_tx(tx_hash) is not None:
            raise AlreadyKnown("transaction already known",
                               details={"tx_hash": tx_hash})

        # 1) 过期
        expires_at = now + self.cfg.ttl_seconds
        if expires_at <= now:
            # ttl 配置成 0 的边界：仍记录但立即过期
            pass

        # 2) gas_price 地板
        if tx.gas_price < MIN_GAS_PRICE:
            raise Underpriced(f"gas_price {tx.gas_price} below minimum {MIN_GAS_PRICE}",
                              details={"gas_price": tx.gas_price, "minimum": MIN_GAS_PRICE})

        account = self.repo.get_account(sender)
        balance = int(account["balance"]) if account else 0
        onchain_nonce = account["nonce"] if account else 0

        # 3) nonce 范围
        if tx.nonce < onchain_nonce:
            raise NonceTooLow(
                f"nonce {tx.nonce} below account nonce {onchain_nonce}",
                details={"nonce": tx.nonce, "account_nonce": onchain_nonce})
        if tx.nonce > onchain_nonce + self.cfg.max_future_nonce:
            raise NonceTooFar(
                f"nonce {tx.nonce} too far in future (limit "
                f"{onchain_nonce}+{self.cfg.max_future_nonce})",
                details={"nonce": tx.nonce, "account_nonce": onchain_nonce,
                         "max_future_nonce": self.cfg.max_future_nonce})

        # 4) 同 nonce 替换（pending/queued/included 均占槽）
        incumbent = self.repo.get_active(sender, tx.nonce)
        if incumbent is not None:
            required = (incumbent.gas_price * (100 + self.cfg.price_bump_pct) + 99) // 100
            if tx.gas_price < required:
                raise ReplacementUnderpriced(
                    f"replacement gas_price {tx.gas_price} does not meet "
                    f"{self.cfg.price_bump_pct}% bump over {incumbent.gas_price} "
                    f"(need >= {required})",
                    details={"incumbent": incumbent.tx_hash,
                             "old_gas_price": incumbent.gas_price,
                             "new_gas_price": tx.gas_price,
                             "required_gas_price": required,
                             "bump_pct": self.cfg.price_bump_pct})
            # 替换者若会让可执行前缀变得负担不起则拒绝：计入同 nonce 之前已
            # pending 前缀的预估花费，而不是只看单笔余额。
            projected = self._projected_balance(sender, balance, onchain_nonce, tx.nonce)
            max_cost = tx.value + tx.gas_limit * tx.gas_price
            if incumbent.status == PENDING and projected < max_cost:
                raise InsufficientFunds(
                    f"replacement max_cost {max_cost} exceeds projected balance {projected}",
                    details={"balance": balance, "projected": projected,
                             "max_cost": max_cost})

        # 5) 容量控制。注意：替换会先释放槽位，故容量检查在替换之后。
        global_active = self.repo.count_status(("pending", "queued"))
        if incumbent is None and global_active >= self.cfg.max_global:
            raise PoolFull(
                f"global pool full ({global_active}/{self.cfg.max_global})",
                details={"active": global_active, "max_global": self.cfg.max_global})

        q_count = self.repo.count_status((QUEUED,))
        # 新交易若将落在 queued：保守判定——无法在分类前精确知道，但只要当前
        # 全局 queued 已满，就必须能淘汰出一个严格更便宜的 queued 才能进。
        will_replace = incumbent is not None
        if incumbent is None and q_count >= self.cfg.max_global_queued:
            victim = self.repo.cheapest_queued()
            if victim is None or victim.gas_price >= tx.gas_price:
                raise PoolFull(
                    "global queued pool full and no strictly cheaper queued tx to evict",
                    details={"queued": q_count,
                             "max_global_queued": self.cfg.max_global_queued,
                             "incoming_gas_price": tx.gas_price,
                             "victim_gas_price": victim.gas_price if victim else None})

        account_q = self.repo.count_sender_status(sender, (QUEUED,))
        # 账户 queued 容量：仅当新交易不是“落进可执行前缀”的交易时约束。
        # 保守规则：账户 queued 已满时，需要淘汰一个本账户严格更便宜的 queued。
        if incumbent is None and account_q >= self.cfg.max_account_queued:
            victim = self.repo.cheapest_queued_for_sender(sender)
            if victim is None or victim.gas_price >= tx.gas_price:
                raise AccountQueueFull(
                    f"sender queued full ({account_q}/{self.cfg.max_account_queued}) "
                    "and no cheaper own queued tx to evict",
                    details={"sender": to_checksum_address(sender),
                             "queued": account_q,
                             "max_account_queued": self.cfg.max_account_queued})

        # 6) 替换先归档旧交易（唯一索引要求插入新行前释放 (sender,nonce) 槽位）。
        if incumbent is not None:
            self.repo.set_tx_status(
                incumbent.tx_hash, "replaced", REASON_REPLACED_PRICE,
                f"replaced by {tx_hash}: {incumbent.gas_price} -> {tx.gas_price} "
                f"(bump rule {self.cfg.price_bump_pct}%)",
                now, replaced_by=tx_hash,
                block_number=incumbent.block_number, position=incumbent.position)
            self._journal(request_id=request_id, action="replace", tx=incumbent,
                          from_status=incumbent.status, to_status="replaced",
                          reason=REASON_REPLACED_PRICE,
                          detail={"replaced_by": tx_hash,
                                  "old_gas_price": incumbent.gas_price,
                                  "new_gas_price": tx.gas_price})

        # 7) 写库
        fields = {
            "tx_hash": tx_hash,
            "raw": tx.to_rlp(),
            "sender": sender,
            "to_addr": ("0x" + tx.to.hex()) if tx.to else None,
            "nonce": tx.nonce,
            "gas_price": str(tx.gas_price),
            "gas_limit": tx.gas_limit,
            "value": str(tx.value),
            "data": tx.data,
            "chain_id": tx.chain_id,
            "received_at": now,
            "expires_at": expires_at,
            "status": QUEUED,      # 先入等待，classify_sender 立即重新归类
            "reason": REASON_NONCE_GAP,
            "reason_detail": "initial insert pending classification",
            "replaced_by": None,
            "block_number": None,
            "position": None,
            "updated_at": now,
        }
        try:
            self.repo.insert_tx(fields)
        except UniqueActiveViolation as exc:  # 理论上前面已查，兜底防并发/竞态
            raise AlreadyKnown("active transaction with same (sender, nonce)",
                               details={"sender": to_checksum_address(sender),
                                        "nonce": tx.nonce}) from exc
        new_rec = self.repo.get_tx(tx_hash)
        self._journal(request_id=request_id, action="receive", tx=new_rec,
                      from_status="", to_status=QUEUED, reason="RECEIVED",
                      detail={"gas_price": tx.gas_price, "expires_at": expires_at})

        # 8) 容量淘汰（插入后若超出配额，淘汰比新交易便宜的 queued）
        if incumbent is None:
            if self.repo.count_status((QUEUED,)) > self.cfg.max_global_queued:
                victim = self.repo.cheapest_queued()
                if victim is not None and victim.gas_price < tx.gas_price:
                    self._evict_one_queued(
                        exclude_sender=None, request_id=request_id,
                        reason=REASON_EVICTED_QUEUE_GLOBAL,
                        detail=f"global queued cap {self.cfg.max_global_queued}; "
                               f"victim price {victim.gas_price} < incoming {tx.gas_price}")
            if self.repo.count_sender_status(sender, (QUEUED,)) > self.cfg.max_account_queued:
                victim = self.repo.cheapest_queued_for_sender(sender)
                if victim is not None and victim.gas_price < tx.gas_price:
                    self._evict_one_queued(
                        exclude_sender=None, request_id=request_id,
                        reason=REASON_EVICTED_QUEUE_ACCOUNT,
                        detail=f"sender queued cap {self.cfg.max_account_queued}")

        # 9) 重新分类该发送者
        classified = self.classify_sender(sender, request_id=request_id)
        final = self.repo.get_tx(tx_hash)
        if final.status == "expired":
            # ttl=0 边界：立即过期，不返还错误，但明确告知
            return {"tx_hash": tx_hash, "status": "expired",
                    "reason": REASON_EXPIRED_TTL,
                    "warnings": ["transaction expired immediately (ttl<=0)"]}
        queued_reasons = {h: r for h, r in classified["queued"]}
        return {
            "tx_hash": tx_hash,
            "status": final.status,
            "reason": final.reason if final.status == PENDING else queued_reasons.get(tx_hash, final.reason),
        }

    # ------------------------------------------------------------------ #
    # 候选区块选择
    # ------------------------------------------------------------------ #
    def candidate_order(self, gas_limit: int) -> list[TxRecord]:
        """按确定性规则给出候选区块内交易顺序。

        每一步在**各发送者当前队首 pending 交易**中选 gas_price 最高者；
        某笔因 gas 剩余不足放不下时跳过该发送者（其后续 nonce 不能越过它），
        等其他发送者处理完后不再重试。同价时按发送者地址升序、再按 nonce
        升序，结果完全确定，可逐笔与黄金文件比对。

        费用高但自身有 nonce 缺口的交易根本不会进入 pending，因此不可能
        被选中跳过缺口。
        """
        heads: dict[str, list[TxRecord]] = {}
        for sender in self.repo.pending_senders():
            pending = self.repo.list_sender(sender, (PENDING,))
            if pending:
                heads[sender] = sorted(pending, key=lambda t: t.nonce)

        # 堆元素：(-gas_price, sender, nonce, tx_hash, tx)
        heap: list = []
        for sender, txs in heads.items():
            t = txs[0]
            heapq.heappush(heap, (-t.gas_price, sender, t.nonce, t.tx_hash, t))

        ordered: list[TxRecord] = []
        skipped_senders: set[str] = set()
        gas_used = 0
        while heap:
            neg_price, sender, nonce, _, t = heapq.heappop(heap)
            if sender in skipped_senders:
                continue
            # 固有 gas 由 data 长度重算（与编码模块同规则）
            from ..encoding import intrinsic_gas
            needed = intrinsic_gas(t.data)
            if gas_used + needed > gas_limit:
                # 该发送者队首放不下 => 其后续也不许越过
                skipped_senders.add(sender)
                continue
            ordered.append(t)
            gas_used += needed
            txs = heads[sender]
            idx = next(i for i, x in enumerate(txs) if x.tx_hash == t.tx_hash)
            if idx + 1 < len(txs):
                nxt = txs[idx + 1]
                heapq.heappush(heap, (-nxt.gas_price, sender, nxt.nonce, nxt.tx_hash, nxt))
        return ordered
