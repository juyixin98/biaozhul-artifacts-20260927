"""链状态内核：候选区块提议、确认、丢弃、回滚重新分类。

区块生命周期
~~~~~~~~~~~~
``propose``  -> 产生 status=proposed 的区块，选中交易 pending -> included（余额不动）
``confirm``  -> 唯一的 pending 区块原子上链：
                逐笔校验 contiguous nonce 与余额 -> 快照 -> 扣余额/转 value/付 fee/
                递增 nonce -> included -> mined；同时按确认时刻重新分类剩余交易
``discard``  -> 放弃当前提议区块：included -> pending/queued（重新分类）
``rollback`` -> 逆转最近 n 个**已确认**区块：恢复快照、mined -> 池中重新分类，
                过期者按当前时间重新判定；proposed 区块存在时拒绝回滚，避免悬空引用。

所有跨表写操作都在单个 ``BEGIN IMMEDIATE`` 事务内提交。
"""

from __future__ import annotations

from eth_hash.auto import keccak

from ..clock import Clock
from ..config import ChainConfig
from ..encoding import intrinsic_gas, to_checksum_address
from ..errors import (
    BlockConflict,
    BlockNotProposed,
    EmptyBlock,
    InvalidState,
    RollbackTooDeep,
)
from . import (
    PENDING,
    QUEUED,
    REASON_BLOCK_DISCARDED,
    REASON_CONFIRMED,
    REASON_EXPIRED_TTL,
    REASON_INCLUDED,
    REASON_ROLLBACK_REEXEC,
)
from .mempool import Mempool
from ..storage.repository import Repository

GENESIS_HASH = "0x" + "00" * 32
ZERO_ADDRESS = "0x" + "00" * 20


class Chain:
    def __init__(self, repo: Repository, pool: Mempool, config: ChainConfig, clock: Clock):
        self.repo = repo
        self.pool = pool
        self.cfg = config
        self.clock = clock

    # ------------------------------------------------------------------ #
    def _journal(self, *, request_id: str, action: str,
                 tx_hash: str | None = None, sender: str | None = None,
                 block_number: int | None = None,
                 from_status: str = "", to_status: str = "",
                 reason: str = "", detail: dict | None = None) -> None:
        self.repo.add_journal(
            ts=self.clock.now(), request_id=request_id, action=action,
            tx_hash=tx_hash, sender=sender, block_number=block_number,
            from_status=from_status, to_status=to_status, reason=reason,
            detail=detail,
        )

    def head_number(self) -> int:
        head = self.repo.head_block()
        return head["number"] if head else 0

    def head_hash(self) -> str:
        head = self.repo.head_block()
        return head["hash"] if head else GENESIS_HASH

    # ------------------------------------------------------------------ #
    def candidate_preview(self, gas_limit: int | None = None) -> dict:
        """只读预览候选区块顺序与逐笔入选/落选理由（不产生任何状态变更）。"""
        gas_limit = gas_limit or self.cfg.block_gas_limit
        selected = self.pool.candidate_order(gas_limit)
        gas_used = sum(intrinsic_gas(t.data) for t in selected)
        return {
            "gas_limit": gas_limit,
            "gas_used": gas_used,
            "order": [
                {
                    "position": i,
                    "tx_hash": t.tx_hash,
                    "sender": to_checksum_address(t.sender),
                    "nonce": t.nonce,
                    "gas_price": str(t.gas_price),
                    "reason": REASON_INCLUDED,
                }
                for i, t in enumerate(selected)
            ],
        }

    # ------------------------------------------------------------------ #
    def propose(self, *, gas_limit: int | None = None, coinbase: str | None = None,
                request_id: str = "") -> dict:
        gas_limit = gas_limit or self.cfg.block_gas_limit
        now = self.clock.now()

        with self.repo.transaction():
            # 过期先清（候选区块内的 included 交易若已过期也释放出来）
            expired = self.pool.reap_expired(request_id=request_id)
            for h in expired:
                rec = self.repo.get_tx(h)
                if rec and rec.block_number is not None:
                    self.repo.clear_block_assignment(h)

            open_proposal = self.repo.latest_by_status("proposed")
            if open_proposal is not None:
                raise BlockConflict(
                    f"block {open_proposal['number']} is already proposed and not resolved",
                    details={"open_block_number": open_proposal["number"]})

            selected = self.pool.candidate_order(gas_limit)
            if not selected:
                raise EmptyBlock("no executable pending transactions for a candidate block")

            number = self.repo.tip_block()["number"] + 1 if self.repo.tip_block() else 1
            parent_hash = self.head_hash()
            gas_used = sum(intrinsic_gas(t.data) for t in selected)
            coinbase = (coinbase or ZERO_ADDRESS).lower()
            block_hash = "0x" + keccak(
                bytes.fromhex(parent_hash[2:])
                + b"".join(bytes.fromhex(t.tx_hash[2:]) for t in selected)
                + number.to_bytes(8, "big")
            ).hex()

            self.repo.insert_block(number, block_hash, parent_hash, gas_limit, gas_used,
                                   coinbase, now)
            for pos, t in enumerate(selected):
                self.repo.set_tx_status(
                    t.tx_hash, "included", REASON_INCLUDED,
                    f"selected at position {pos} of proposed block {number}",
                    now, block_number=number, position=pos)
                self.repo.insert_block_tx(number, pos, t.tx_hash)
                self._journal(request_id=request_id, action="include",
                              tx_hash=t.tx_hash, sender=t.sender, block_number=number,
                              from_status=PENDING, to_status="included",
                              reason=REASON_INCLUDED,
                              detail={"position": pos, "gas_price": t.gas_price})
            self._journal(request_id=request_id, action="propose", block_number=number,
                          reason=REASON_INCLUDED,
                          detail={"hash": block_hash, "parent": parent_hash,
                                  "tx_count": len(selected), "gas_used": gas_used})

        return {"number": number, "hash": block_hash, "parent_hash": parent_hash,
                "gas_limit": gas_limit, "gas_used": gas_used, "coinbase": coinbase,
                "transactions": [t.tx_hash for t in selected]}

    # ------------------------------------------------------------------ #
    def confirm(self, block_number: int | None = None, *, request_id: str = "") -> dict:
        now = self.clock.now()
        with self.repo.transaction():
            proposed = self.repo.latest_by_status("proposed")
            if proposed is None:
                raise BlockNotProposed("there is no proposed block to confirm")
            if block_number is not None and block_number != proposed["number"]:
                raise BlockConflict(
                    f"only the latest proposed block may be confirmed: "
                    f"asked {block_number}, open is {proposed['number']}",
                    details={"asked": block_number, "open": proposed["number"]})
            number = proposed["number"]
            if number != self.head_number() + 1:
                raise InvalidState(
                    f"proposed block {number} is not a child of head {self.head_number()}")

            tx_hashes = self.repo.list_block_txs(number)
            txs = [self.repo.get_tx(h) for h in tx_hashes]

            # ---- 预演：全部可执行才提交（contiguous nonce + 余额） ---- #
            # 按发送者分组并保持区块内顺序；同发送者在块内必然按 nonce 递增。
            pending_balance: dict[str, int] = {}
            pending_nonce: dict[str, int] = {}
            for pos, t in enumerate(txs):
                if t is None or t.status != "included" or t.block_number != number:
                    raise InvalidState(
                        f"tx {getattr(t, 'tx_hash', None)} is not stably included in {number}")
                acct = self.repo.get_account(t.sender)
                balance = int(acct["balance"]) if acct else 0
                nonce = acct["nonce"] if acct else 0
                balance = pending_balance.get(t.sender, balance)
                nonce = pending_nonce.get(t.sender, nonce)
                if t.nonce != nonce:
                    raise InvalidState(
                        f"block {number} tx at position {pos} has nonce {t.nonce} but "
                        f"sender nonce would be {nonce}; confirmation refuses non-contiguous block",
                        details={"position": pos, "tx_nonce": t.nonce, "expect": nonce})
                cost = t.value + t.gas_limit * t.gas_price
                if balance < cost:
                    raise InvalidState(
                        f"sender {t.sender} cannot cover position {pos}: "
                        f"balance {balance} < cost {cost}",
                        details={"balance": balance, "cost": cost})
                pending_balance[t.sender] = balance - cost
                pending_nonce[t.sender] = nonce + 1

            # ---- 快照所有受影响账户（包括收款方/coinbase） ---- #
            coinbase = proposed["coinbase"]
            touched = {t.sender for t in txs} | {t.to_addr for t in txs if t.to_addr} | {coinbase}
            for addr in touched:
                self.repo.ensure_account(addr, now)
                self.repo.snapshot_account(number, addr)

            # ---- 应用状态转移（逐笔：sender 扣 value+fee，receiver 收 value，
            #      全部处理完后 coinbase 收手续费总额；nonce 逐笔递增） ---- #
            total_fees = 0
            for t in txs:
                fee = t.gas_limit * t.gas_price
                total_fees += fee
                self.repo.adjust_balance(t.sender, -(t.value + fee), now)
                if t.to_addr:
                    self.repo.adjust_balance(t.to_addr, t.value, now)
                acct = self.repo.get_account(t.sender)
                self.repo.set_account(t.sender, int(acct["balance"]), acct["nonce"] + 1, now)
            self.repo.adjust_balance(coinbase, total_fees, now)

            self.repo.confirm_block(number, now)
            for t in txs:
                self.repo.set_tx_status(
                    t.tx_hash, "mined", REASON_CONFIRMED,
                    f"mined in confirmed block {number}", now,
                    block_number=number, position=t.position)
                self._journal(request_id=request_id, action="mine",
                              tx_hash=t.tx_hash, sender=t.sender, block_number=number,
                              from_status="included", to_status="mined",
                              reason=REASON_CONFIRMED,
                              detail={"position": t.position})

            # 确认后所有仍有池中交易的发送者重新分类（nonce/余额已变）
            affected_senders = {t.sender for t in txs}
            reclass = {}
            for sender in affected_senders:
                reclass[sender] = self.pool.classify_sender(
                    sender, request_id=request_id, reason=REASON_CONFIRMED)
            self._journal(request_id=request_id, action="confirm", block_number=number,
                          reason=REASON_CONFIRMED,
                          detail={"tx_count": len(txs), "total_fees": total_fees})

        return {"number": number, "hash": proposed["hash"], "status": "confirmed",
                "transactions": tx_hashes, "total_fees": str(total_fees)}

    # ------------------------------------------------------------------ #
    def discard(self, *, request_id: str = "") -> dict:
        with self.repo.transaction():
            proposed = self.repo.latest_by_status("proposed")
            if proposed is None:
                raise BlockNotProposed("there is no proposed block to discard")
            number = proposed["number"]
            tx_hashes = self.repo.list_block_txs(number)

            for h in tx_hashes:
                t = self.repo.get_tx(h)
                if t.status != "included":
                    continue
                now = self.clock.now()
                # 可能在 included 期间过期
                if t.expires_at <= now:
                    self.repo.set_tx_status(
                        h, "expired", REASON_EXPIRED_TTL,
                        f"expired while included (expires_at={t.expires_at})", now)
                    self._journal(request_id=request_id, action="expire",
                                  tx_hash=h, sender=t.sender, block_number=number,
                                  from_status="included", to_status="expired",
                                  reason=REASON_EXPIRED_TTL,
                                  detail={"expires_at": t.expires_at})
                else:
                    self.repo.set_tx_status(
                        h, QUEUED, REASON_BLOCK_DISCARDED,
                        f"block {number} discarded; return to pool for reclassification",
                        now)
                    self._journal(request_id=request_id, action="uninclude",
                                  tx_hash=h, sender=t.sender, block_number=number,
                                  from_status="included", to_status=QUEUED,
                                  reason=REASON_BLOCK_DISCARDED,
                                  detail={"block_number": number})
            self.repo.delete_block(number)
            self._journal(request_id=request_id, action="discard", block_number=number,
                          reason=REASON_BLOCK_DISCARDED,
                          detail={"tx_count": len(tx_hashes)})

            senders = {self.repo.get_tx(h).sender for h in tx_hashes}
            reclass = {}
            for sender in senders:
                rec = self.repo.list_sender(sender, (PENDING, QUEUED, "expired"))
                if any(x.status in (PENDING, QUEUED) for x in rec):
                    reclass[sender] = self.pool.classify_sender(
                        sender, request_id=request_id, reason=REASON_BLOCK_DISCARDED)

        return {"discarded_number": number, "transactions": tx_hashes}

    # ------------------------------------------------------------------ #
    def rollback(self, n: int = 1, *, request_id: str = "") -> dict:
        if n < 1:
            raise RollbackTooDeep("rollback depth must be >= 1")
        with self.repo.transaction():
            if self.repo.latest_by_status("proposed") is not None:
                raise BlockConflict("resolve the proposed block before rolling back")
            head = self.repo.head_block()
            if head is None or n > head["number"]:
                raise RollbackTooDeep(
                    f"cannot roll back {n} blocks: chain height is "
                    f"{head['number'] if head else 0}")

            restored_tx_hashes: list[str] = []
            reverted_numbers: list[int] = []
            for number in range(head["number"], head["number"] - n, -1):
                block = self.repo.get_block(number)
                if block is None or block["status"] != "confirmed":
                    raise InvalidState(f"block {number} is not confirmed; cannot roll back")
                tx_hashes = self.repo.list_block_txs(number)

                # 1) 恢复快照（逆向恢复时，同一账户只恢复到本批最老快照一次：
                #    逐块恢复会得到更早状态，顺序执行天然正确——先恢复较新块再较老块）
                snaps = self.repo.snapshots(number)
                for snap in snaps:
                    self.repo.set_account(
                        snap["address"], int(snap["balance_before"]),
                        snap["nonce_before"], self.clock.now())

                # 2) mined -> queued（随后统一重新分类）
                now = self.clock.now()
                for h in tx_hashes:
                    t = self.repo.get_tx(h)
                    if t.expires_at <= now:
                        self.repo.set_tx_status(
                            h, "expired", REASON_EXPIRED_TTL,
                            f"already expired at rollback (expires_at={t.expires_at})",
                            now)
                        self._journal(request_id=request_id, action="expire",
                                      tx_hash=h, sender=t.sender, block_number=number,
                                      from_status="mined", to_status="expired",
                                      reason=REASON_EXPIRED_TTL,
                                      detail={"rollback_of_block": number})
                    else:
                        self.repo.set_tx_status(
                            h, QUEUED, REASON_ROLLBACK_REEXEC,
                            f"block {number} rolled back; re-enter pool", now,
                            clear_assignment=True)
                        restored_tx_hashes.append(h)
                    self._journal(request_id=request_id, action="unmine",
                                  tx_hash=h, sender=t.sender, block_number=number,
                                  from_status="mined",
                                  to_status="expired" if t.expires_at <= now else QUEUED,
                                  reason=REASON_EXPIRED_TTL if t.expires_at <= now
                                  else REASON_ROLLBACK_REEXEC,
                                  detail={"rollback_of_block": number})

                self.repo.delete_snapshots(number)
                self.repo.delete_block(number)
                reverted_numbers.append(number)

            # 3) 受影响发送者全部重新分类
            senders = {self.repo.get_tx(h).sender for h in restored_tx_hashes}
            reclass = {}
            for sender in sorted(senders):
                reclass[sender] = self.pool.classify_sender(
                    sender, request_id=request_id, reason=REASON_ROLLBACK_REEXEC)
            self._journal(request_id=request_id, action="rollback",
                          reason=REASON_ROLLBACK_REEXEC,
                          detail={"reverted": reverted_numbers,
                                  "restored_tx": restored_tx_hashes})

        return {"reverted_blocks": reverted_numbers,
                "restored_transactions": restored_tx_hashes}
