"""链状态内核：接收区块 -> 验证 -> 按父哈希连接（未知父先挂起）->
按固定权重规则维护最佳链 -> 以"先撤旧、后加新"维护可撤回派生索引。

所有状态变更在单个 IMMEDIATE 事务内完成；最终性边界以内的重组直接拒绝，
不会触碰派生表，因此查询永远只见某一条完整链的版本，不会读到切换中间态。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from . import consensus, crypto
from .config import Settings
from .errors import (
    ConsensusRuleError,
    DecodeError,
    DuplicateBlockError,
    FinalityReorgError,
    UnknownParentError,
    VerificationError,
)
from .models import Block, IngestOutcome
from .storage import Storage


class ChainKernel:
    def __init__(self, storage: Storage, settings: Settings, diagnostics=None):
        self.storage = storage
        self.settings = settings
        self.diag = diagnostics
        self.storage.bootstrap_seq()
        # 测试钩子：在切换事务内、撤旧完成而加新之前调用；抛异常即可模拟切换中断。
        self.mid_switch_hook: Callable[[Any, list[str], list[str]], None] | None = None

    # ------------------------------------------------------------- 诊断

    def _event(self, event: str, request_id: str | None, level=logging.INFO,
               conn=None, **fields) -> str:
        # 持久审计总是写入 diag_events（即使未配置 stderr 日志器）。
        if self.diag is not None:
            event_id = self.diag.emit(event, request_id=request_id, level=level, **fields)
        else:
            event_id = f"evt-{self.storage.next_seq():06d}"
        self.storage.log_diag(conn, event_id, request_id, event, fields)
        return event_id

    def _reject_event(self, exc: DomainError, request_id: str | None,
                      level: int = logging.WARNING, **fixed) -> None:
        """记录拒绝诊断；context 中与固定字段重名的键以固定字段为准。

        拒绝可能发生在某个已回滚事务之后，因此这里用独立事务确保审计落盘。
        """

        payload = {k: v for k, v in exc.context.items() if k not in fixed}
        fields = {"reason": exc.category, "detail": exc.message, **payload, **fixed}
        if self.diag is not None:
            self.diag.emit("block_rejected", request_id=request_id, level=level, **fields)
        with self.storage.transaction() as conn:
            self.storage.log_diag(conn, f"evt-{self.storage.next_seq():06d}",
                                 request_id, "block_rejected", fields)

    # ------------------------------------------------------------- 验证

    def _verify_block(self, block: dict) -> Block:
        model = Block.from_dict(block)
        for tx in model.txs:
            crypto.verify_tx(tx.to_dict())
        crypto.verify_block_header(model.header.to_dict(), [t.to_dict() for t in model.txs])
        return model

    # ------------------------------------------------------------- 查询

    def tip_hash(self) -> str | None:
        return self.storage.get_tip()

    def _require_block(self, block_hash: str) -> dict:
        row = self.storage.get_block(block_hash)
        if row is None:
            raise DecodeError("区块未知", block_hash=block_hash)
        return row

    def ancestry(self, block_hash: str) -> list[str]:
        """沿父哈希回溯到创世，返回高度降序的区块哈希列表。"""

        chain: list[str] = []
        cursor = block_hash
        seen: set[str] = set()
        while cursor != crypto.ZERO_HASH:
            if cursor in seen:
                raise ConsensusRuleError("父哈希出现环", block_hash=cursor)
            seen.add(cursor)
            row = self.storage.get_block(cursor)
            if row is None:
                raise DecodeError("回溯时遇到未知父区块", block_hash=cursor)
            chain.append(cursor)
            cursor = row["prev_hash"]
        return chain

    def canonical_chain(self) -> list[str]:
        """当前最佳链，高度升序。"""

        tip = self.tip_hash()
        return [] if tip is None else list(reversed(self.ancestry(tip)))

    def on_canonical(self, block_hash: str) -> bool:
        return block_hash in set(self.canonical_chain())

    def confirmation_depth(self, block_hash: str) -> int | None:
        """确认深度（链尖为 1）；不在当前最佳链上返回 None。"""

        tip = self.tip_hash()
        if tip is None:
            return None
        if not self.on_canonical(block_hash):
            return None
        tip_row = self._require_block(tip)
        row = self._require_block(block_hash)
        return consensus.confirmations(row["height"], tip_row["height"])

    def is_finalized(self, block_hash: str) -> bool:
        tip = self.tip_hash()
        if tip is None or not self.on_canonical(block_hash):
            return False
        return consensus.is_finalized(
            self._require_block(block_hash)["height"],
            self._require_block(tip)["height"],
            self.settings.finality_depth,
        )

    def finalized_height(self) -> int | None:
        tip = self.tip_hash()
        if tip is None:
            return None
        # 链短于最终性深度时尚无最终确定区块，高度下界为 0。
        return max(0, self._require_block(tip)["height"] - self.settings.finality_depth)

    def get_balance(self, address: str) -> int:
        return self.storage.get_balance(address)

    def all_balances(self) -> dict[str, int]:
        return self.storage.all_balances()

    def state_summary(self) -> dict:
        tip = self.tip_hash()
        summary: dict[str, Any] = {
            "tip_hash": tip,
            "tip_height": None,
            "tip_cumulative_weight": None,
            "finality_depth": self.settings.finality_depth,
            "max_reorg_depth": self.settings.max_reorg_depth,
            "finalized_height": self.finalized_height(),
            "canonical_chain": self.canonical_chain(),
            "pending_count": self.storage.pending_count(),
            "balances": self.all_balances(),
            "contribution_count": self.storage.contribution_count(),
        }
        if tip is not None:
            row = self._require_block(tip)
            summary["tip_height"] = row["height"]
            summary["tip_cumulative_weight"] = row["cumulative_weight"]
        return summary

    # ------------------------------------------------------------- 写入

    def ingest(self, block: dict, request_id: str | None = None) -> IngestOutcome:
        """接收一个区块。拒绝路径抛 errors 中的类型错误；返回挂起/接受结论。"""

        # 1) 结构与密码学验证（在任何状态变更之前）。
        try:
            model = self._verify_block(block)
        except (DecodeError, VerificationError, ConsensusRuleError) as exc:
            self._reject_event(exc, request_id)
            raise
        header = model.header.to_dict()
        block_hash = header["block_hash"]

        self._event(
            "block_received", request_id,
            block_hash=block_hash, height=header["height"],
            parent_hash=header["prev_hash"], weight=header["weight"],
        )

        if self.storage.block_exists(block_hash):
            exc = DuplicateBlockError("区块已存在（权威链、分叉或悬挂池中）", block_hash=block_hash)
            self._reject_event(exc, request_id, level=logging.INFO,
                               block_hash=block_hash, height=header["height"])
            raise exc

        # 2) 父连接检查：创世块直接落根；未知父先挂起（不是拒绝）。
        if header["height"] == 0:
            if self.storage.genesis_exists():
                exc = ConsensusRuleError("已存在创世块，拒绝第二个根", block_hash=block_hash)
                self._reject_event(exc, request_id, block_hash=block_hash)
                raise exc
        elif self.storage.get_block(header["prev_hash"]) is None:
            with self.storage.transaction() as conn:
                seq = self.storage.next_seq()
                self.storage.add_pending(conn, model.to_dict(), seq)
                self._event("block_pending", request_id, conn=conn,
                            block_hash=block_hash, height=header["height"],
                            missing_parent=header["prev_hash"],
                            pending_count=self.storage.pending_count())
            return IngestOutcome(
                status="pending", block_hash=block_hash, height=header["height"],
                parent_hash=header["prev_hash"], pending=True,
                pending_count=self.storage.pending_count(),
                reason=f"未知父区块 {header['prev_hash']}，已挂起",
            )

        # 3) 连接并考虑链选择（含随后的悬挂块排空）。
        try:
            return self._connect_and_drain(model.to_dict(), request_id)
        except (FinalityReorgError, ConsensusRuleError) as exc:
            self._reject_event(exc, request_id,
                               block_hash=block_hash, height=header["height"])
            raise

    def _connect_and_drain(self, block: dict, request_id: str | None) -> IngestOutcome:
        """在单个事务内连接区块、决定链尖、排空新变可连接的悬挂块。

        返回首个区块的结论（状态与回滚区间反映第一个触发的动作）；
        末尾附上排空后最终链尖。排空过程中任意合法性错误都会回滚整个事务，
        保证调用方不可能读到部分排空的中间状态。
        """

        with self.storage.transaction() as conn:
            outcome = self._connect_one(conn, block, request_id)
            frontier = [outcome.block_hash]
            while frontier:
                parent = frontier.pop(0)
                for child in self.storage.take_pending_children(conn, parent):
                    child_outcome = self._connect_one(conn, child, request_id)
                    outcome.canonical_changed = outcome.canonical_changed or child_outcome.canonical_changed
                    outcome.connected.extend(child_outcome.connected)
                    outcome.disconnected.extend(child_outcome.disconnected)
                    frontier.append(child_outcome.block_hash)
            tip = self.storage.get_tip()
            if tip is not None:
                tip_row = self.storage.get_block(tip)
                outcome.tip_hash, outcome.tip_height = tip, tip_row["height"]
                outcome.tip_weight = tip_row["cumulative_weight"]
                if outcome.status == "fork_kept" and outcome.disconnected:
                    outcome.status = "switched"
            return outcome

    def _connect_one(self, conn, block: dict, request_id: str | None) -> IngestOutcome:
        header = block["header"]
        block_hash = header["block_hash"]
        parent = self.storage.get_block(header["prev_hash"]) if header["height"] > 0 else None
        if header["height"] > 0:
            if parent is None:
                raise UnknownParentError("父区块缺失，应处于挂起池", block_hash=block_hash)
            if header["height"] != parent["height"] + 1:
                raise ConsensusRuleError(
                    "高度与父区块不连续", height=header["height"],
                    parent_height=parent["height"], block_hash=block_hash,
                )
        cumulative = header["weight"] if parent is None else parent["cumulative_weight"] + header["weight"]
        seq = self.storage.next_seq()
        self.storage.insert_block(conn, block, seq, cumulative)

        tip_hash = self.storage.get_tip()
        # 第一条链（或接在当前链尖之后）：直接延伸。
        if tip_hash is None:
            added = self.storage.apply_block(conn, block_hash, block["txs"])
            self.storage.set_tip(conn, block_hash)
            self._event("block_accepted", request_id, conn=conn, decision="extended",
                        block_hash=block_hash, height=header["height"], contributions_added=added)
            return IngestOutcome(status="extended", block_hash=block_hash, height=header["height"],
                                 parent_hash=header["prev_hash"], canonical_changed=True,
                                 tip_hash=block_hash, tip_height=header["height"], tip_weight=cumulative,
                                 connected=[block_hash])

        tip_row = self.storage.get_block(tip_hash)
        candidate_ancestry = self.ancestry(block_hash)
        incumbent_ancestry = self.ancestry(tip_hash)
        common = self._split_point(candidate_ancestry, incumbent_ancestry)
        common_row = self.storage.get_block(common)

        cand_segment = cumulative - common_row["cumulative_weight"]
        inc_segment = tip_row["cumulative_weight"] - common_row["cumulative_weight"]
        switch = consensus.should_switch(cand_segment, inc_segment, self.settings.tie_keep_canonical)

        if common == tip_hash:
            # 候选是当前链尖的后代：必然延伸（段权重为正）。
            switch = True

        if not switch:
            self._event("block_accepted", request_id, conn=conn, decision="fork_kept",
                        block_hash=block_hash, height=header["height"],
                        candidate_segment_weight=cand_segment,
                        incumbent_segment_weight=inc_segment,
                        tie=cand_segment == inc_segment)
            return IngestOutcome(
                status="fork_kept", block_hash=block_hash, height=header["height"],
                parent_hash=header["prev_hash"], canonical_changed=False,
                tip_hash=tip_hash, tip_height=tip_row["height"],
                tip_weight=tip_row["cumulative_weight"],
                reason=f"候选段权重 {cand_segment} <= 当前段权重 {inc_segment}，保留当前链",
            )

        connected, disconnected = self._switch_paths(
            conn, block_hash, common, cand_segment, inc_segment, request_id,
        )
        rollback_from = self.storage.get_block(disconnected[0])["height"] if disconnected else None
        rollback_to = self.storage.get_block(disconnected[-1])["height"] if disconnected else None
        return IngestOutcome(
            status="switched" if disconnected else "extended",
            block_hash=block_hash, height=header["height"],
            parent_hash=header["prev_hash"], canonical_changed=True,
            tip_hash=block_hash, tip_height=header["height"], tip_weight=cumulative,
            disconnected=disconnected, connected=connected,
            rollback_from_height=rollback_from, rollback_to_height=rollback_to,
        )

    @staticmethod
    def _split_point(ancestry_a: list[str], ancestry_b: list[str]) -> str:
        """两条降序祖先链的最近共同区块。"""

        set_b = set(ancestry_b)
        for h in ancestry_a:
            if h in set_b:
                return h
        raise ConsensusRuleError("两条链没有共同祖先，无法比较")

    def _switch_paths(self, conn, new_tip: str, split: str,
                      cand_weight: int, inc_weight: int,
                      request_id: str | None) -> tuple[list[str], list[str]]:
        """先撤旧、后加新；越最终性边界则拒绝（不产生任何派生变更）。"""

        old_tip = self.storage.get_tip()

        # 祖先按"链尖->创世"排列，必须在分叉点处截断：分叉点及其更早的共同
        # 祖先既不撤回也不重连，否则会错误撤掉创世块。
        def until_split(anc: list[str]) -> list[str]:
            prefix: list[str] = []
            for h in anc:
                if h == split:
                    break
                prefix.append(h)
            return prefix

        disconnected = until_split(self.ancestry(old_tip))   # 高度降序
        connected = list(reversed(until_split(self.ancestry(new_tip))))  # 高度升序

        # 最终性闸门：在撤回任何贡献之前判定。
        consensus.ensure_reorg_allowed(
            len(disconnected), self.settings.finality_depth,
            old_tip=old_tip, new_tip=new_tip, split=split,
        )

        self._event(
            "reorg_begin" if disconnected else "extend_begin", request_id, conn=conn,
            old_tip=old_tip, new_tip=new_tip,
            split=split, rollback_count=len(disconnected), connect_count=len(connected),
            candidate_segment_weight=cand_weight, incumbent_segment_weight=inc_weight,
            rollback_from_height=self.storage.get_block(disconnected[0])["height"] if disconnected else None,
            rollback_to_height=self.storage.get_block(disconnected[-1])["height"] if disconnected else None,
        )

        # 先撤旧：从旧链尖向分叉点逐块撤回。
        for h in disconnected:
            removed = self.storage.unapply_block(conn, h)
            self._event("block_disconnected", request_id, conn=conn,
                        block_hash=h, height=self.storage.get_block(h)["height"],
                        contributions_removed=removed)

        if self.mid_switch_hook is not None:
            # 模拟切换中断：仍在事务内，异常会触发整体回滚，派生索引恢复为旧链版本。
            self.mid_switch_hook(conn, disconnected, connected)

        # 后加新：从分叉点之后第一块向新链尖逐块应用。
        for h in connected:
            raw = self.storage.get_block_raw(h)
            added = self.storage.apply_block(conn, h, raw["txs"])
            self._event("block_connected", request_id, conn=conn,
                        block_hash=h, height=raw["header"]["height"],
                        contributions_added=added)

        self.storage.set_tip(conn, new_tip)
        if disconnected:
            # 只在真正发生重组（撤掉过旧块）时落 reorg 审计；纯延伸不算。
            self.storage.log_reorg(
                conn, request_id, old_tip, new_tip, disconnected, connected,
                self.storage.get_block(disconnected[0])["height"],
                self.storage.get_block(disconnected[-1])["height"],
            )
        return connected, disconnected
