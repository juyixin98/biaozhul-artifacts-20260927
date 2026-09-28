"""链状态内核：交易池策略与状态迁移的唯一权威实现。

所有迁移都在单个 SQLite 事务内完成，审计行与索引行同事务提交。
状态分类::

    pending  = 可执行连续前缀（nonce 恰为账户当前 nonce 起，连续无缺口）
    queued   = 缺口之后等待
    proposed = 已进入（尚未最终确定的）区块
    confirmed / dropped = 终态（dropped 保留行以便审计）

核心不变量（``Repository.assert_integrity`` 会复核）:

* 同 (sender, nonce) 有效交易唯一；
* pending 永远是连续前缀，高费不能跨过自身 nonce 缺口；
* 账户 projected_balance 差额 == pending+proposed 承诺花费之和。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from eth_hash.auto import keccak
from eth_utils import encode_hex

from ..storage.repository import Repository
from .clock import Clock, FakeClock, SystemClock
from .config import Config
from .crypto import intrinsic_gas
from .models import (
    AccountState,
    Block,
    DropReason,
    ErrorCode,
    StoredTransaction,
    Transaction,
    TxError,
    TxStatus,
)
from .ordering import CandidatePlan, OrderingConfig, SenderContext, build_candidate_plan

GENESIS_HASH = "0x" + "00" * 32


@dataclass
class OperationResult:
    """一次状态变更操作的可解释结果。"""

    accepted: bool
    tx_hash: str | None
    status: TxStatus | None = None
    reason: str = ""
    error_code: ErrorCode | None = None
    # 该请求触发的全部审计事件 id（与 GET /audit/requests/{id} 对应）
    audit_ids: list[int] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


def new_request_id(prefix: str = "req") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Kernel:
    def __init__(
        self,
        repo: Repository,
        config: Config,
        clock: Clock | None = None,
    ) -> None:
        self.repo = repo
        self.config = config
        self.clock: Clock = clock or SystemClock()

    # ============================ 基础工具 ============================ #
    def now_ms(self) -> int:
        return self.clock.now_ms()

    def _audit(
        self,
        conn,
        *,
        request_id: str,
        event_type: str,
        reason: str,
        tx_hash: str | None = None,
        block_hash: str | None = None,
        detail: dict[str, Any] | None = None,
        module: str = "kernel",
    ) -> int:
        return self.repo.append_audit(
            conn,
            request_id=request_id,
            now_ms=self.now_ms(),
            event_type=event_type,
            reason=reason,
            tx_hash=tx_hash,
            block_hash=block_hash,
            detail=detail,
            module=module,
        )

    # ============================ 账户 ============================ #
    def create_or_fund_account(
        self,
        address: str,
        balance: int,
        *,
        request_id: str | None = None,
        reset_nonce: bool = False,
    ) -> AccountState:
        request_id = request_id or new_request_id("fund")
        with self.repo.transaction() as conn:
            existing = self.repo.get_account(conn, address)
            if existing is not None and not reset_nonce:
                acct = self.repo.credit_account(conn, existing.address, balance)
                self._audit(
                    conn,
                    request_id=request_id,
                    event_type="account_funded",
                    reason="credit_existing",
                    detail={"address": acct.address, "credited": balance,
                            "balance": acct.balance},
                )
            else:
                acct = self.repo.upsert_account(
                    conn, address, balance,
                    nonce=0 if existing is None else existing.nonce,
                )
                self._audit(
                    conn,
                    request_id=request_id,
                    event_type="account_created",
                    reason="create",
                    detail={"address": acct.address, "balance": acct.balance},
                )
            return acct

    def get_account(self, address: str) -> AccountState | None:
        with self.repo.transaction() as conn:
            return self.repo.get_account(conn, address)

    def list_accounts(self) -> list[AccountState]:
        with self.repo.transaction() as conn:
            return self.repo.list_accounts(conn)

    # ============================ 静态校验 ============================ #
    def _static_validate(self, tx: Transaction) -> None:
        g = self.config.gas
        if len(tx.data) > g.max_data_bytes:
            raise TxError(
                ErrorCode.DATA_TOO_LARGE,
                f"data 长度 {len(tx.data)} 超过上限 {g.max_data_bytes}",
                {"size": len(tx.data), "limit": g.max_data_bytes},
            )
        required_gas = intrinsic_gas(
            tx, base_gas=g.tx_intrinsic_gas, per_data_byte=g.data_gas_per_byte
        )
        if tx.gas_limit < required_gas:
            raise TxError(
                ErrorCode.INTRINSIC_GAS_TOO_LOW,
                f"gas_limit {tx.gas_limit} < 内在 gas {required_gas}",
                {"gas_limit": tx.gas_limit, "required": required_gas},
            )
        if tx.gas_limit > g.block_gas_limit:
            raise TxError(
                ErrorCode.GAS_LIMIT_EXCEEDS_BLOCK,
                f"gas_limit {tx.gas_limit} 超过区块上限 {g.block_gas_limit}",
                {"gas_limit": tx.gas_limit, "block_limit": g.block_gas_limit},
            )
        if tx.gas_price < g.min_gas_price_wei:
            raise TxError(
                ErrorCode.GAS_PRICE_BELOW_MINIMUM,
                f"gas_price {tx.gas_price} 低于最低 {g.min_gas_price_wei}",
                {"gas_price": tx.gas_price,
                 "minimum": g.min_gas_price_wei},
            )

    def tx_cost(self, tx: Transaction) -> int:
        return tx.gas_limit * tx.gas_price + tx.value

    # ============================ 重分类 ============================ #
    def _recompute_projected(self, conn, sender: str) -> None:
        """权威重算 projected_balance。

        口径：``projected = balance - Σ pending 承诺花费``。
        proposed 交易的费用在出块时**已实际从 balance 扣除**（回滚时才退回），
        因此不能再从 projected 里预留一次，否则双重计算。该口径与
        ``Repository.assert_integrity`` 完全一致：回滚/确认/替换后调用一次即可。
        """
        acct = self.repo.require_account(conn, sender)
        row = conn.execute(
            """
            SELECT COALESCE(SUM(gas_limit*gas_price + value), 0) committed
            FROM transactions
            WHERE sender=? AND status='pending'
            """,
            (sender,),
        ).fetchone()
        projected = acct.balance - int(row["committed"])
        conn.execute(
            "UPDATE accounts SET projected_balance=? WHERE address=?",
            (projected, sender),
        )

    def _reclassify_sender(
        self, conn, request_id: str, sender: str
    ) -> list[StoredTransaction]:
        """把一个账户的池内交易重新切成 pending 连续前缀 + queued 缺口队列。

        pending 的定义（比单纯 nonce 连续更严格）：从账户当前 nonce 起，
        nonce 连续 **且** 累计承诺花费不超过余额的最长前缀。一旦遇到 nonce
        缺口或余额无法覆盖的交易，其后全部落入 queued——高费同样不能越过。
        proposed/confirmed/dropped 不参与。迁移逐条写 status_change 审计。
        """
        acct = self.repo.require_account(conn, sender)
        pool = self.repo.txs_for_sender(
            conn, sender, [TxStatus.PENDING, TxStatus.QUEUED]
        )
        by_nonce = {s.tx.nonce: s for s in pool}

        desired: dict[int, TxStatus] = {}
        next_nonce = acct.nonce
        cumulative = 0
        while next_nonce in by_nonce:
            stored = by_nonce[next_nonce]
            cost = self.tx_cost(stored.tx)
            if cumulative + cost > acct.balance:
                # 余额截止点：本笔及之后全部 queued。
                break
            desired[next_nonce] = TxStatus.PENDING
            cumulative += cost
            next_nonce += 1
        for stored in pool:
            if stored.tx.nonce not in desired:
                desired[stored.tx.nonce] = TxStatus.QUEUED

        changed: list[StoredTransaction] = []
        for stored in sorted(pool, key=lambda s: s.tx.nonce):
            wanted = desired[stored.tx.nonce]
            if wanted is TxStatus.PENDING:
                reason = (
                    "prefix_advanced"
                    if stored.tx.nonce == acct.nonce
                    else "prefix_continued"
                )
            else:
                # 精确区分两类阻塞：nonce 缺口 vs 余额截止
                missing = [
                    n for n in range(acct.nonce, stored.tx.nonce)
                    if n not in by_nonce
                ]
                reason = (
                    "queued_behind_gap" if missing
                    else "queued_balance_cutoff"
                )
            if wanted is not stored.status:
                self.repo.update_tx_status(
                    conn,
                    stored.tx.tx_hash,
                    wanted,
                    self.now_ms(),
                    reason,
                )
                self._audit(
                    conn,
                    request_id=request_id,
                    event_type="status_change",
                    reason=reason,
                    tx_hash=stored.tx.tx_hash,
                    detail={"from": stored.status.value, "to": wanted.value,
                            "nonce": stored.tx.nonce, "account_nonce": acct.nonce},
                )
                changed.append(
                    StoredTransaction(
                        tx=stored.tx,
                        status=wanted,
                        received_at_ms=stored.received_at_ms,
                        status_reason=reason,
                    )
                )
            elif stored.status_reason != reason:
                # 状态未变（例如新插入即为 QUEUED），但把权威理由写正，
                # 让"移入理由"对每一条都可解释。
                conn.execute(
                    "UPDATE transactions SET status_reason=? WHERE tx_hash=?",
                    (reason, stored.tx.tx_hash),
                )
                changed.append(
                    StoredTransaction(
                        tx=stored.tx,
                        status=wanted,
                        received_at_ms=stored.received_at_ms,
                        status_reason=reason,
                    )
                )

        self._recompute_projected(conn, sender)
        return changed

    # ============================ 淘汰 ============================ #
    def _evict_one(
        self,
        conn,
        request_id: str,
        *,
        reason: DropReason,
        include_pending: bool,
        protect_hashes: set[str] | None = None,
    ) -> StoredTransaction | None:
        protect_hashes = protect_hashes or set()
        for candidate in self.repo.eviction_candidates(
            conn, include_pending=include_pending
        ):
            if candidate.tx.tx_hash in protect_hashes:
                continue
            self.repo.update_tx_status(
                conn,
                candidate.tx.tx_hash,
                TxStatus.DROPPED,
                self.now_ms(),
                reason.value,
                drop_reason=reason,
            )
            self._audit(
                conn,
                request_id=request_id,
                event_type="evicted",
                reason=reason.value,
                tx_hash=candidate.tx.tx_hash,
                detail={"gas_price": candidate.tx.gas_price,
                        "nonce": candidate.tx.nonce,
                        "previous_status": candidate.status.value},
            )
            if candidate.status is TxStatus.PENDING:
                # 释放承诺并重排连续前缀（淘汰 pending 会让 queued 前移）。
                self._recompute_projected(conn, candidate.tx.sender)
                self._reclassify_sender(conn, request_id, candidate.tx.sender)
            return candidate
        return None

    # ============================ 准入 ============================ #
    def submit_transaction(
        self,
        tx: Transaction,
        *,
        request_id: str | None = None,
    ) -> OperationResult:
        """入口：静态校验 -> 链id/签名在解码层已验 -> 余额/nonce/容量 -> 落库重分类。"""

        request_id = request_id or new_request_id("submit")
        with self.repo.transaction() as conn:
            try:
                self._static_validate(tx)

                sender = self.repo.get_account(conn, tx.sender)
                if sender is None:
                    raise TxError(
                        ErrorCode.MALFORMED_TRANSACTION,
                        f"未知发送者账户 {tx.sender}；请先创建合成账户",
                        {"sender": tx.sender},
                    )

                # 完全相同的交易（同哈希）
                existing_any = self.repo.get_by_hash_any_status(conn, tx.tx_hash)
                if existing_any is not None and existing_any.status in (
                    TxStatus.PENDING,
                    TxStatus.QUEUED,
                    TxStatus.PROPOSED,
                ):
                    raise TxError(
                        ErrorCode.SAME_TRANSACTION_KNOWN,
                        "相同交易已在池中",
                        {"tx_hash": tx.tx_hash, "status": existing_any.status.value},
                    )

                # 同 sender/nonce 已有有效交易 -> RBF 路径
                incumbent = self.repo.get_active_by_sender_nonce(
                    conn, tx.sender, tx.nonce
                )
                if incumbent is not None:
                    return self._apply_replacement(
                        conn, request_id, incumbent=incumbent, new_tx=tx
                    )

                # nonce 边界
                if tx.nonce < sender.nonce:
                    raise TxError(
                        ErrorCode.NONCE_TOO_LOW,
                        f"nonce {tx.nonce} < 账户当前 nonce {sender.nonce}",
                        {"nonce": tx.nonce, "account_nonce": sender.nonce},
                    )
                max_future = sender.nonce + self.config.pool.max_queued_per_sender
                if tx.nonce > max_future:
                    raise TxError(
                        ErrorCode.NONCE_TOO_FAR_AHEAD,
                        f"nonce {tx.nonce} 超过未来槽位上限 {max_future}",
                        {"nonce": tx.nonce, "max_future_nonce": max_future},
                    )

                pending_n, queued_n = self.repo.sender_pool_counts(
                    conn, tx.sender
                )
                if pending_n + queued_n >= self.config.pool.max_transactions_per_sender:
                    raise TxError(
                        ErrorCode.SENDER_SLOT_LIMIT,
                        "该发送者有效交易数已达上限",
                        {"sender": tx.sender,
                         "limit": self.config.pool.max_transactions_per_sender},
                    )

                cost = self.tx_cost(tx)
                # queued 交易（nonce 超前）不在此处承诺余额——它的可执行性在
                # 缺口闭合、重分类时统一判定（余额截止点）。
                if tx.nonce == sender.nonce and sender.projected_balance < cost:
                    raise TxError(
                        ErrorCode.INSUFFICIENT_FUNDS,
                        "可承诺余额不足",
                        {
                            "cost": cost,
                            "projected_balance": sender.projected_balance,
                            "balance": sender.balance,
                        },
                    )

                # 容量：淘汰到有空位。只驱逐 queued；除非配置允许最后手段驱逐 pending。
                if self.repo.pool_count(conn) >= self.config.pool.max_transactions:
                    freed = self._evict_one(
                        conn, request_id,
                        reason=DropReason.EVICTED_CAPACITY,
                        include_pending=False,
                        protect_hashes={tx.tx_hash},
                    )
                    if freed is None and self.config.pool.evict_pending_as_last_resort:
                        freed = self._evict_one(
                            conn, request_id,
                            reason=DropReason.EVICTED_CAPACITY,
                            include_pending=True,
                            protect_hashes={tx.tx_hash},
                        )
                    if self.repo.pool_count(conn) >= self.config.pool.max_transactions:
                        raise TxError(
                            ErrorCode.POOL_FULL,
                            "交易池已满且无法通过淘汰腾出空间",
                            {"capacity": self.config.pool.max_transactions},
                        )

                # 落库：新交易先一律 QUEUED，随后由重分类权威决定前缀归属，
                # 保证 pending 连续且累计余额可承担。
                self.repo.insert_transaction(
                    conn, tx, TxStatus.QUEUED, self.now_ms(),
                    "admitted_pending_reclassify"
                )
                self._audit(
                    conn,
                    request_id=request_id,
                    event_type="admitted",
                    reason="admitted",
                    tx_hash=tx.tx_hash,
                    detail={"nonce": tx.nonce, "gas_price": tx.gas_price,
                            "cost": cost},
                )
                self._reclassify_sender(conn, request_id, tx.sender)
                final_state = self.repo.get_tx(conn, tx.tx_hash)
                return OperationResult(
                    accepted=True,
                    tx_hash=tx.tx_hash,
                    status=final_state.status if final_state else TxStatus.QUEUED,
                    reason=final_state.status_reason if final_state else "admitted",
                    audit_ids=self._audit_ids_for(conn, request_id),
                )
            except TxError as exc:
                self._audit(
                    conn,
                    request_id=request_id,
                    event_type="admission_rejected",
                    reason=exc.code.value,
                    tx_hash=tx.tx_hash,
                    detail={"message": exc.message, **exc.details},
                )
                return OperationResult(
                    accepted=False,
                    tx_hash=tx.tx_hash,
                    status=None,
                    reason=exc.message,
                    error_code=exc.code,
                    audit_ids=self._audit_ids_for(conn, request_id),
                    detail=exc.details,
                )

    def _audit_ids_for(self, conn, request_id: str, *extra: int) -> list[int]:
        rows = conn.execute(
            "SELECT audit_id FROM audit_log WHERE request_id=? ORDER BY audit_id",
            (request_id,),
        ).fetchall()
        return [r["audit_id"] for r in rows]

    # ============================ RBF 替换 ============================ #
    def _apply_replacement(
        self,
        conn,
        request_id: str,
        *,
        incumbent: StoredTransaction,
        new_tx: Transaction,
    ) -> OperationResult:
        """同 (sender,nonce) 替换：必须满足明确涨价条件，且二者不并存有效。

        只允许替换池内 pending/queued。proposed（已进入未确认区块）的交易
        必须先通过区块回滚重组，避免"同一笔既在区块里又被换掉"的歧义。
        """
        if incumbent.status is TxStatus.PROPOSED:
            raise TxError(
                ErrorCode.CONFLICT,
                "旧交易已在未确认区块中，请先回滚该区块再替换",
                {"incumbent": incumbent.tx.tx_hash,
                 "incumbent_status": incumbent.status.value},
            )

        bump_pct = self.config.pool.replacement_price_bump_pct
        required_price = (
            incumbent.tx.gas_price * (100 + bump_pct) + 99
        ) // 100  # 向上取整
        if new_tx.gas_price < required_price:
            raise TxError(
                ErrorCode.SAME_NONCE_LOWER_PRICE,
                f"替换价格不达标：新 gas_price {new_tx.gas_price} "
                f"需 >= {required_price}（旧价 {incumbent.tx.gas_price}，"
                f"至少涨价 {bump_pct}%）",
                {
                    "old_gas_price": incumbent.tx.gas_price,
                    "new_gas_price": new_tx.gas_price,
                    "required_gas_price": required_price,
                    "bump_pct": bump_pct,
                },
            )

        sender = self.repo.require_account(conn, new_tx.sender)
        # 若旧交易在 pending（占用承诺额），评估新交易在同一 nonce 位上的
        # 可承担性：临时把旧成本还回 projected，再要求新成本可覆盖。
        old_cost = self.tx_cost(incumbent.tx)
        new_cost = self.tx_cost(new_tx)
        if incumbent.status is TxStatus.PENDING:
            available = sender.projected_balance + old_cost
            if available < new_cost:
                raise TxError(
                    ErrorCode.INSUFFICIENT_FUNDS,
                    "替换后新交易的承诺额超出可承担余额",
                    {"new_cost": new_cost, "available": available},
                )

        self.repo.update_tx_status(
            conn,
            incumbent.tx.tx_hash,
            TxStatus.DROPPED,
            self.now_ms(),
            DropReason.REPLACED.value,
            drop_reason=DropReason.REPLACED,
        )
        self._audit(
            conn,
            request_id=request_id,
            event_type="replaced",
            reason=DropReason.REPLACED.value,
            tx_hash=incumbent.tx.tx_hash,
            detail={"replaced_by": new_tx.tx_hash,
                    "old_gas_price": incumbent.tx.gas_price,
                    "new_gas_price": new_tx.gas_price,
                    "old_status": incumbent.status.value},
        )

        self.repo.insert_transaction(
            conn, new_tx, TxStatus.QUEUED, self.now_ms(),
            "admitted_as_replacement"
        )
        self._audit(
            conn,
            request_id=request_id,
            event_type="admitted",
            reason="replacement_admitted",
            tx_hash=new_tx.tx_hash,
            detail={"nonce": new_tx.nonce, "gas_price": new_tx.gas_price,
                    "replaces": incumbent.tx.tx_hash},
        )
        self._reclassify_sender(conn, request_id, new_tx.sender)
        final_state = self.repo.get_tx(conn, new_tx.tx_hash)
        return OperationResult(
            accepted=True,
            tx_hash=new_tx.tx_hash,
            status=final_state.status if final_state else TxStatus.QUEUED,
            reason="replacement_admitted",
            audit_ids=self._audit_ids_for(conn, request_id),
            detail={"replaces": incumbent.tx.tx_hash},
        )

    # ============================ 过期 ============================ #
    def expire_pending(
        self, *, request_id: str | None = None
    ) -> list[str]:
        """按模拟时钟使超时 pending 失效，并修复受影响账户的连续前缀。"""
        request_id = request_id or new_request_id("expire")
        ttl = self.config.pool.pending_ttl_seconds
        if ttl <= 0:
            return []
        cutoff = self.now_ms() - ttl * 1000
        expired: list[str] = []
        affected_senders: set[str] = set()
        with self.repo.transaction() as conn:
            victims = self.repo.expired_pending(conn, cutoff)
            for stored in victims:
                self.repo.update_tx_status(
                    conn,
                    stored.tx.tx_hash,
                    TxStatus.DROPPED,
                    self.now_ms(),
                    DropReason.EXPIRED.value,
                    drop_reason=DropReason.EXPIRED,
                )
                affected_senders.add(stored.tx.sender)
                self._audit(
                    conn,
                    request_id=request_id,
                    event_type="expired",
                    reason=DropReason.EXPIRED.value,
                    tx_hash=stored.tx.tx_hash,
                    detail={
                        "pending_age_ms": (
                            self.now_ms() - stored.received_at_ms
                        ),
                        "ttl_ms": ttl * 1000,
                    },
                )
                expired.append(stored.tx.tx_hash)
            # 过期只可能打击 pending：逐发送者重算承诺并重排连续前缀，
            # 后面的 queued 可能在缺口闭合后前移，索引不会留下空洞。
            for sender in affected_senders:
                self._recompute_projected(conn, sender)
                self._reclassify_sender(conn, request_id, sender)
        return expired

    # ============================ 候选/提议区块 ============================ #
    def preview_candidate(self) -> dict[str, Any]:
        """只读预览候选顺序，不改变任何状态。"""
        with self.repo.transaction() as conn:
            pending = self.repo.pending_txs(conn)
            ctx = self._build_sender_ctx(conn, pending)
            plan = build_candidate_plan(
                pending,
                sender_ctx=ctx,
                config=OrderingConfig(
                    block_gas_limit=self.config.gas.block_gas_limit
                ),
            )
            return {
                "ordered": [
                    {
                        "tx_hash": s.tx.tx_hash,
                        "sender": s.tx.sender,
                        "nonce": s.tx.nonce,
                        "gas_price": s.tx.gas_price,
                        "gas_limit": s.tx.gas_limit,
                    }
                    for s in plan.ordered
                ],
                "skipped": [
                    {"tx_hash": sk.tx_hash, "reason": sk.reason, **sk.detail}
                    for sk in plan.skipped
                ],
                "gas_used": plan.gas_used,
            }

    def _build_sender_ctx(self, conn, pending: list[StoredTransaction]) -> dict[str, SenderContext]:
        ctx: dict[str, SenderContext] = {}
        for s in pending:
            ctx.setdefault(
                s.tx.sender,
                SenderContext(
                    address=s.tx.sender,
                    nonce=self.repo.require_account(conn, s.tx.sender).nonce,
                    available_balance=self.repo.require_account(
                        conn, s.tx.sender
                    ).balance,
                ),
            )
        return ctx

    def propose_block(
        self,
        *,
        request_id: str | None = None,
        external_txs: list[Transaction] | None = None,
    ) -> tuple[Block, CandidatePlan, list[tuple[Transaction, TxError]]]:
        """组装并应用一个新区块（候选顺序执行；external_txs 用于外部块夹具）。

        执行语义（简化，见 README"限制"）：按计划顺序逐笔执行，
        从余额扣除 ``value + gas_limit*gas_price``（全额预扣），nonce +1；
        执行期失败（余额不足等）交易跳过，不改变状态。
        区块哈希 = keccak(parent_hash || number || 有序 tx_hash)。
        """
        request_id = request_id or new_request_id("propose")
        external_txs = external_txs or []
        rejected: list[tuple[Transaction, TxError]] = []

        with self.repo.transaction() as conn:
            head = self.repo.head_block(conn)
            parent_hash = head.block_hash if head else GENESIS_HASH
            number = (head.number + 1) if head else self.config.chain.genesis_number + 1

            # 外部交易：未在池中的先尝试完整准入（复用同一套规则+审计）。
            for etx in external_txs:
                stored = self.repo.get_tx(conn, etx.tx_hash)
                if stored is None or stored.status in (
                    TxStatus.DROPPED,
                    TxStatus.CONFIRMED,
                    TxStatus.ROLLED_BACK,
                ):
                    # 以独立子请求身份走准入；失败收集到 rejected，不阻断出块。
                    sub_id = f"{request_id}.ext-{etx.tx_hash[:10]}"
                    result = self._submit_external(conn, etx, sub_id)
                    if not result.accepted:
                        rejected.append(
                            (
                                etx,
                                TxError(
                                    result.error_code or ErrorCode.CONFLICT,
                                    result.reason,
                                    result.detail,
                                ),
                            )
                        )

            pending = self.repo.pending_txs(conn)
            ctx = self._build_sender_ctx(conn, pending)
            plan = build_candidate_plan(
                pending,
                sender_ctx=ctx,
                config=OrderingConfig(
                    block_gas_limit=self.config.gas.block_gas_limit
                ),
            )
            if not plan.ordered:
                raise TxError(
                    ErrorCode.BLOCK_FULL,
                    "候选区块为空：没有可执行交易",
                )

            positions: list[tuple[str, bool]] = []
            applied_hashes: list[str] = []
            gas_used = 0
            # 块内运行时状态：起始 nonce 用计划前快照（绝不能在循环里重读
            # account.nonce——它会随执行前进），每发送者已执行笔数与实时余额。
            start_nonce = {a: c.nonce for a, c in ctx.items()}
            applied_count: dict[str, int] = {}
            running_balance: dict[str, int] = {}
            touched_senders: set[str] = set()
            for stored in plan.ordered:
                tx = stored.tx
                acct = self.repo.require_account(conn, tx.sender)
                base_nonce = start_nonce.get(tx.sender, acct.nonce)
                expected_nonce = base_nonce + applied_count.get(tx.sender, 0)
                available = running_balance.get(tx.sender, acct.balance)
                cost = self.tx_cost(tx)
                if tx.nonce != expected_nonce or available < cost:
                    # 执行期失败：保留池内状态，区块记录 skipped（正常流程不应发生，
                    # 因为候选引擎已保证连续与可承担；这里是最后一道防线）。
                    positions.append((tx.tx_hash, False))
                    self._audit(
                        conn,
                        request_id=request_id,
                        event_type="execution_skipped",
                        reason="nonce_or_balance_mismatch",
                        tx_hash=tx.tx_hash,
                        detail={"expected_nonce": expected_nonce,
                                "nonce": tx.nonce,
                                "available": available, "cost": cost},
                    )
                    continue
                new_balance = available - cost
                running_balance[tx.sender] = new_balance
                applied_count[tx.sender] = applied_count.get(tx.sender, 0) + 1
                touched_senders.add(tx.sender)
                conn.execute(
                    "UPDATE accounts SET balance=?, nonce=nonce+1 "
                    "WHERE address=?",
                    (new_balance, tx.sender),
                )
                applied_hashes.append(tx.tx_hash)
                positions.append((tx.tx_hash, True))
                gas_used += tx.gas_limit

            block_hash = self._block_hash(
                parent_hash, number, [h for h, applied in positions]
            )
            block = Block(
                block_hash=block_hash,
                number=number,
                parent_hash=parent_hash,
                proposed_at_ms=self.now_ms(),
                executed_tx_hashes=tuple(applied_hashes),
                skipped_tx_hashes=tuple(
                    h for h, applied in positions if not applied
                ),
                gas_used=gas_used,
            )
            self.repo.insert_block(conn, block, positions, confirmed=False)

            # 已应用交易 -> proposed；执行期跳过的保持 pending。
            for h in applied_hashes:
                self.repo.update_tx_status(
                    conn, h, TxStatus.PROPOSED, self.now_ms(),
                    "included_in_proposed_block", proposed_block=block_hash,
                )
            # proposed 仍占用承诺（未最终确定，回滚会退款）：重算后重分类，
            # 让被解锁的 queued 前移。
            for sender in touched_senders:
                self._recompute_projected(conn, sender)
            self._audit(
                conn,
                request_id=request_id,
                event_type="block_proposed",
                reason="proposed",
                block_hash=block_hash,
                detail={
                    "number": number,
                    "parent_hash": parent_hash,
                    "applied": applied_hashes,
                    "skipped": [h for h, applied in positions if not applied],
                    "gas_used": gas_used,
                },
            )

            # 应用后各发送者可能解锁 queued -> pending。
            for s in self.repo.active_senders(conn):
                self._reclassify_sender(conn, request_id, s)

            self._auto_confirm(conn, request_id)
            return block, plan, rejected

    def _submit_external(self, conn, tx: Transaction, sub_id: str) -> OperationResult:
        """外部块交易的内部准入（共享同一事务与审计）。"""
        try:
            self._static_validate(tx)
            sender = self.repo.get_account(conn, tx.sender)
            if sender is None:
                raise TxError(
                    ErrorCode.MALFORMED_TRANSACTION,
                    f"外部交易来自未知账户 {tx.sender}",
                    {"sender": tx.sender},
                )
            existing = self.repo.get_active_by_sender_nonce(
                conn, tx.sender, tx.nonce
            )
            if existing is not None:
                if existing.tx.tx_hash == tx.tx_hash:
                    return OperationResult(
                        accepted=True, tx_hash=tx.tx_hash,
                        status=existing.status, reason="external_already_in_pool",
                    )
                return self._apply_replacement(
                    conn, sub_id, incumbent=existing, new_tx=tx
                )
            if tx.nonce < sender.nonce:
                raise TxError(
                    ErrorCode.NONCE_TOO_LOW,
                    f"外部交易 nonce {tx.nonce} < 账户 nonce {sender.nonce}",
                    {"nonce": tx.nonce, "account_nonce": sender.nonce},
                )
            if tx.nonce > sender.nonce + self.config.pool.max_queued_per_sender:
                raise TxError(
                    ErrorCode.NONCE_TOO_FAR_AHEAD, "外部交易 nonce 超前过多",
                    {"nonce": tx.nonce},
                )
            cost = self.tx_cost(tx)
            # 与普通入口一致：仅对当前 nonce 的交易做余额承诺检查；
            # 超前 nonce 由重分类在缺口闭合时统一判定。
            if tx.nonce == sender.nonce and sender.projected_balance < cost:
                raise TxError(
                    ErrorCode.INSUFFICIENT_FUNDS, "外部交易余额不足",
                    {"cost": cost, "projected_balance": sender.projected_balance},
                )
            self.repo.insert_transaction(
                conn, tx, TxStatus.QUEUED, self.now_ms(), "external_admitted"
            )
            self._audit(
                conn, request_id=sub_id, event_type="admitted",
                reason="external_admitted", tx_hash=tx.tx_hash,
                detail={"nonce": tx.nonce},
            )
            self._recompute_projected(conn, tx.sender)
            self._reclassify_sender(conn, sub_id, tx.sender)
            final_state = self.repo.get_tx(conn, tx.tx_hash)
            return OperationResult(
                accepted=True, tx_hash=tx.tx_hash,
                status=final_state.status if final_state else TxStatus.QUEUED,
                reason="external_admitted",
            )
        except TxError as exc:
            self._audit(
                conn, request_id=sub_id, event_type="admission_rejected",
                reason=exc.code.value, tx_hash=tx.tx_hash,
                detail={"message": exc.message, **exc.details, "external": True},
            )
            return OperationResult(
                accepted=False, tx_hash=tx.tx_hash, reason=exc.message,
                error_code=exc.code, detail=exc.details,
            )

    @staticmethod
    def _block_hash(
        parent_hash: str, number: int, ordered_hashes: list[str]
    ) -> str:
        preimage = (
            parent_hash.encode()
            + number.to_bytes(8, "big")
            + b"".join(h.encode() for h in ordered_hashes)
        )
        return encode_hex(keccak(preimage))

    # ============================ 确认 ============================ #
    def _auto_confirm(self, conn, request_id: str) -> list[str]:
        """把链头下方确认深度以外的 proposed 区块标记 confirmed。"""
        depth = self.config.finality.confirmation_depth
        head = conn.execute(
            "SELECT MAX(number) m FROM blocks"
        ).fetchone()["m"]
        if head is None:
            return []
        cutoff = head - depth
        rows = conn.execute(
            "SELECT block_hash FROM blocks WHERE number <= ? AND confirmed=0",
            (cutoff,),
        ).fetchall()
        hashes = [r["block_hash"] for r in rows]
        if not hashes:
            return []
        self.repo.mark_confirmed(conn, hashes)
        marks = ",".join("?" for _ in hashes)
        # 先取受影响交易与发送者，再做终态更新。
        tx_rows = conn.execute(
            f"SELECT tx_hash, sender FROM transactions "
            f"WHERE proposed_block IN ({marks}) AND status='proposed'",
            hashes,
        ).fetchall()
        confirmed_entries = [(r["tx_hash"], r["sender"]) for r in tx_rows]
        confirmed_senders = sorted({sender for _, sender in confirmed_entries})
        conn.execute(
            f"UPDATE transactions SET status='confirmed', status_reason='depth_finalized', "
            f"updated_at_ms=? WHERE proposed_block IN ({marks}) AND status='proposed'",
            (self.now_ms(), *hashes),
        )
        for h, _ in confirmed_entries:
            self._audit(
                conn, request_id=request_id,
                event_type="confirmed", reason="depth_finalized",
                tx_hash=h, detail={"depth": depth},
            )
        # 已确认交易离开 pending/proposed 集合：承诺口径改变，逐账户重算。
        for sender in confirmed_senders:
            self._recompute_projected(conn, sender)
        return hashes

    def confirm_depth(self, *, request_id: str | None = None) -> list[str]:
        request_id = request_id or new_request_id("confirm")
        with self.repo.transaction() as conn:
            return self._auto_confirm(conn, request_id)

    # ============================ 回滚 ============================ #
    def rollback_to(
        self,
        target_number: int,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """回滚所有 ``number > target_number`` 的区块并重新分类。

        已最终确定（链头下方 >= confirmation_depth）的区块拒绝回滚，
        返回 ``BLOCK_ROLLBACK_FINALIZED``。回滚按 number 降序逐块进行：
        applied 交易退款、nonce 后退，交易先标 rolled_back 再重入池；
        skipped（执行期失败/被替换摘除）的外部交易尝试重新准入，失败有明确记录。
        """
        request_id = request_id or new_request_id("rollback")
        with self.repo.transaction() as conn:
            head_row = conn.execute(
                "SELECT MAX(number) m FROM blocks"
            ).fetchone()
            head_number = head_row["m"]
            if head_number is None:
                # 没有任何区块：幂等 no-op，便于夹具反复执行。
                return {
                    "rolled_back_blocks": [],
                    "reentered": [],
                    "readmitted_external": [],
                    "readmit_failed": [],
                }
            depth = self.config.finality.confirmation_depth
            finalized_floor = head_number - depth
            if target_number < finalized_floor:
                raise TxError(
                    ErrorCode.BLOCK_ROLLBACK_FINALIZED,
                    f"目标高度 {target_number} 已被最终确定 "
                    f"（最多回滚到 {finalized_floor}，确认深度 {depth}）",
                    {"target": target_number,
                     "min_allowed": finalized_floor,
                     "head": head_number},
                )

            blocks = self.repo.blocks_above(conn, target_number)
            reapplied: list[str] = []
            touched_senders: set[str] = set()

            for block in blocks:  # 已按 number 降序
                # 1) applied 交易逆序撤销：退款、nonce 后退，标 rolled_back。
                #    skipped 交易当时未应用，状态本就仍是 pending/queued，不动。
                for h in reversed(block.executed_tx_hashes):
                    stored = self.repo.get_tx(conn, h)
                    if stored is None:
                        continue
                    tx = stored.tx
                    acct = self.repo.require_account(conn, tx.sender)
                    cost = self.tx_cost(tx)
                    conn.execute(
                        "UPDATE accounts SET balance=balance+?, nonce=nonce-1 "
                        "WHERE address=?",
                        (cost, tx.sender),
                    )
                    self.repo.update_tx_status(
                        conn, h, TxStatus.ROLLED_BACK, self.now_ms(),
                        "block_rolled_back",
                    )
                    touched_senders.add(tx.sender)
                    self._audit(
                        conn, request_id=request_id,
                        event_type="rolled_back", reason="block_rolled_back",
                        tx_hash=h, block_hash=block.block_hash,
                        detail={"number": block.number, "refund": cost},
                    )

                self.repo.clear_proposed_block(
                    conn,
                    list(block.executed_tx_hashes) + list(block.skipped_tx_hashes),
                )

                # 2) applied 交易重新进入有效集合（先 QUEUED，由重分类判定）。
                #    回滚恢复了 nonce 与余额，因此它们必然重新有效；
                #    若最终因余额规则落入 queued，审计会给出 queued_* 理由。
                for h in reversed(block.executed_tx_hashes):
                    stored = self.repo.get_tx(conn, h)
                    if stored is None:
                        continue
                    conn.execute(
                        "UPDATE transactions SET status='queued', "
                        "status_reason='reentered_after_rollback', "
                        "updated_at_ms=?, drop_reason=NULL WHERE tx_hash=?",
                        (self.now_ms(), h),
                    )
                    reapplied.append(h)
                    self._audit(
                        conn, request_id=request_id,
                        event_type="reentered_pool",
                        reason="reentered_after_rollback",
                        tx_hash=h, block_hash=block.block_hash,
                    )

                # 3) skipped 交易：执行期失败时从未离开池子，仅记录其重新参与
                #    分类（审计可追溯），不需要任何状态写入。
                for h in block.skipped_tx_hashes:
                    self._audit(
                        conn, request_id=request_id,
                        event_type="rollback_skip_unmoved",
                        reason="skipped_tx_remains_in_pool",
                        tx_hash=h, block_hash=block.block_hash,
                    )

                self.repo.delete_blocks(conn, [block.block_hash])
                self._audit(
                    conn, request_id=request_id,
                    event_type="block_removed", reason="rollback_delete",
                    block_hash=block.block_hash,
                    detail={"number": block.number},
                )

            # 4) 权威重算承诺并重分类（回滚发送者 + 池内所有发送者）。
            for sender in touched_senders:
                self._recompute_projected(conn, sender)
            all_senders = touched_senders | set(self.repo.active_senders(conn))
            for sender in all_senders:
                self._reclassify_sender(conn, request_id, sender)

            self._auto_confirm(conn, request_id)
            return {
                "rolled_back_blocks": [b.block_hash for b in blocks],
                "reentered": reapplied,
                "readmitted_external": [],
                "readmit_failed": [],
            }

    # ============================ 查询 ============================ #
    def get_tx(self, tx_hash: str) -> StoredTransaction | None:
        with self.repo.transaction() as conn:
            return self.repo.get_tx(conn, tx_hash)

    def list_pool(self) -> dict[str, list[dict[str, Any]]]:
        with self.repo.transaction() as conn:
            return self._pool_payload(conn)

    def _pool_payload(self, conn) -> dict[str, list[dict[str, Any]]]:
        from ..storage.repository import _row_to_tx  # 局部导入避免循环

        pending = self.repo.pending_txs(conn)
        queued_rows = conn.execute(
            "SELECT * FROM transactions WHERE status='queued' "
            "ORDER BY sender, nonce"
        ).fetchall()
        return {
            "pending": [self._stored_dict(s) for s in pending],
            "queued": [self._stored_dict(_row_to_tx(r)) for r in queued_rows],
        }

    @staticmethod
    def _stored_dict(s: StoredTransaction) -> dict[str, Any]:
        return {
            "tx_hash": s.tx.tx_hash,
            "sender": s.tx.sender,
            "nonce": s.tx.nonce,
            "gas_price": s.tx.gas_price,
            "gas_limit": s.tx.gas_limit,
            "value": s.tx.value,
            "to": s.tx.to,
            "data_hex": "0x" + s.tx.data.hex(),
            "status": s.status.value,
            "status_reason": s.status_reason,
            "received_at_ms": s.received_at_ms,
        }

    def chain_head(self) -> dict[str, Any]:
        with self.repo.transaction() as conn:
            head = self.repo.head_block(conn)
            height = head.number if head else self.config.chain.genesis_number
            return {
                "height": height,
                "head_hash": head.block_hash if head else GENESIS_HASH,
                "confirmation_depth": self.config.finality.confirmation_depth,
                "finalized_height": max(
                    self.config.chain.genesis_number,
                    height - self.config.finality.confirmation_depth,
                ),
            }

    def audit_events(
        self, *, after_id: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.repo.transaction() as conn:
            return [self._audit_dict(e) for e in
                    self.repo.audit_since(conn, after_id, limit)]

    def audit_for_request(self, request_id: str) -> dict[str, Any]:
        with self.repo.transaction() as conn:
            events = self.repo.audit_for_request(conn, request_id)
            return {
                "request_id": request_id,
                "found": bool(events),
                "events": [self._audit_dict(e) for e in events],
            }

    @staticmethod
    def _audit_dict(e) -> dict[str, Any]:
        return {
            "audit_id": e.audit_id,
            "request_id": e.request_id,
            "occurred_at_ms": e.occurred_at_ms,
            "event_type": e.event_type,
            "tx_hash": e.tx_hash,
            "block_hash": e.block_hash,
            "reason": e.reason,
            "module": e.module,
            "service_version": e.service_version,
            "detail": e.detail,
        }

    def assert_integrity(self) -> None:
        """测试/回放用的全量不变量体检。"""
        with self.repo.transaction() as conn:
            self.repo.assert_integrity(conn)
