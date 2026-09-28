"""独立参考实现（reference oracle）。

刻意 **不** 导入 kernel/storage/consensus——它是评审用来核对被测内核的
"第二来源"。只复用 crypto 中的哈希/验签原语（这些是事实标准，不是链规则）；
选链、权重比较、最终性、去重、累计全部在这里用最直白的方式重写一遍。

行为约定（与内核规格逐条对应）：
- 区块按父哈希连接；父未知则挂起，父到达后按挂起顺序排空；
- 链的相对权重 = 分叉点之后各区块 weight 之和；严格更大才切换，等权不切；
- 回滚数 > finality_depth 的重组明确拒绝，且该块不入链；
- 派生余额沿最终权威链从创世到链尖累计，同一 tx_id 全局只计一次。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import crypto
from .errors import ConsensusRuleError, DecodeError, VerificationError

_CATEGORY_BY_ERROR = {
    DecodeError: "decode_error",
    VerificationError: "verification_error",
    ConsensusRuleError: "consensus_rule",
}


@dataclass
class RefBlock:
    raw: dict
    block_hash: str
    prev_hash: str
    height: int
    weight: int
    txs: list[dict]


@dataclass
class RefReject:
    block_hash: str | None
    category: str
    detail: str


class FinalityViolation(Exception):
    category = "finality_reorg"

    def __init__(self, rollback: int, depth: int, old_tip: str, new_tip: str):
        super().__init__(f"重组需回滚 {rollback} 个区块，超过最终性深度 {depth}")
        self.rollback, self.depth = rollback, depth
        self.old_tip, self.new_tip = old_tip, new_tip


@dataclass
class RefState:
    blocks: dict[str, RefBlock] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    tip: str | None = None
    pending: dict[str, list[dict]] = field(default_factory=dict)
    rejected: list[RefReject] = field(default_factory=list)
    pending_hashes: set[str] = field(default_factory=set)
    finality_rejects: list[dict] = field(default_factory=list)
    switches: list[dict] = field(default_factory=list)

    def is_known(self, block_hash: str) -> bool:
        return block_hash in self.blocks or block_hash in self.pending_hashes

    def ancestry(self, block_hash: str) -> list[str]:
        chain, cur = [], block_hash
        while cur != crypto.ZERO_HASH:
            chain.append(cur)
            cur = self.blocks[cur].prev_hash
        return chain


def _verify(raw: dict) -> None:
    header, txs = raw["header"], raw.get("txs", [])
    for tx in txs:
        crypto.verify_tx(tx)
    crypto.verify_block_header(header, txs)


def _maybe_switch(state: RefState, candidate: RefBlock, finality_depth: int) -> str:
    """决定链尖，返回 extended/switched/fork_kept；越最终性边界抛异常。"""

    if state.tip is None:
        state.tip = candidate.block_hash
        return "extended"
    cand_anc, inc_anc = state.ancestry(candidate.block_hash), state.ancestry(state.tip)
    inc_set = set(inc_anc)
    split = next(h for h in cand_anc if h in inc_set)
    if split == state.tip:
        state.tip = candidate.block_hash
        return "extended"

    def seg_weight(anc: list[str]) -> int:
        total = 0
        for h in anc:
            if h == split:
                break
            total += state.blocks[h].weight
        return total

    cand_w, inc_w = seg_weight(cand_anc), seg_weight(inc_anc)
    if cand_w <= inc_w:
        return "fork_kept"  # 等权保留当前链

    # 祖先按"链尖->创世"排列，在分叉点处截断（分叉点及其更早的共同祖先不属于任一段）。
    def until_split(anc: list[str]) -> list[str]:
        prefix = []
        for h in anc:
            if h == split:
                break
            prefix.append(h)
        return prefix

    disconnected = until_split(inc_anc)
    connected = list(reversed(until_split(cand_anc)))
    if len(disconnected) > finality_depth:
        raise FinalityViolation(len(disconnected), finality_depth, state.tip, candidate.block_hash)
    state.tip = candidate.block_hash
    state.switches.append({"disconnected": disconnected, "connected": connected,
                           "new_tip": candidate.block_hash})
    return "switched"


def _install(state: RefState, raw: dict, finality_depth: int) -> str | None:
    """把一个父已知的块装入并做链选择；最终性拒绝时返回 None 且不入链。"""

    header = raw["header"]
    block_hash = header["block_hash"]
    parent = state.blocks.get(header["prev_hash"])
    if header["height"] > 0:
        if parent is None:
            return "unknown_parent"
        if header["height"] != parent.height + 1:
            state.rejected.append(RefReject(block_hash, "consensus_rule", "高度不连续"))
            return "rejected"
    blk = RefBlock(raw, block_hash, header["prev_hash"], header["height"],
                   header["weight"], raw["txs"])
    state.blocks[block_hash] = blk
    state.order.append(block_hash)
    try:
        return _maybe_switch(state, blk, finality_depth)
    except FinalityViolation as exc:
        # 与内核一致：重组被最终性拒绝时该块不入链，不作为"已知"块保留，
        # 因此重新提交同样按最终性拒绝处理（而非 duplicate）。
        state.finality_rejects.append({"block_hash": block_hash,
                                       "rollback": exc.rollback, "depth": exc.depth})
        state.rejected.append(RefReject(block_hash, "finality_reorg", str(exc)))
        del state.blocks[block_hash]
        state.order.pop()
        return "finality_rejected"


def replay(blocks: list[dict], finality_depth: int = 6) -> RefState:
    state = RefState()
    queue = list(blocks)
    while queue:
        raw = queue.pop(0)
        block_hash = raw["header"]["block_hash"]
        if state.is_known(block_hash):
            state.rejected.append(RefReject(block_hash, "duplicate_block", "区块已存在"))
            continue
        try:
            _verify(raw)
        except (DecodeError, VerificationError, ConsensusRuleError) as exc:
            state.rejected.append(
                RefReject(block_hash, _CATEGORY_BY_ERROR[type(exc)], str(exc)))
            continue
        header = raw["header"]
        if header["height"] > 0 and header["prev_hash"] not in state.blocks:
            state.pending.setdefault(header["prev_hash"], []).append(raw)
            state.pending_hashes.add(block_hash)
            continue
        decision = _install(state, raw, finality_depth)
        if decision in (None, "rejected"):
            continue
        # 排空因该块而变可连接的悬挂块（BFS，保持挂起先后顺序）
        frontier = [block_hash]
        while frontier:
            parent = frontier.pop(0)
            for child in state.pending.pop(parent, []):
                state.pending_hashes.discard(child["header"]["block_hash"])
                child_decision = _install(state, child, finality_depth)
                if child_decision not in (None, "rejected",
                                          "unknown_parent", "finality_rejected"):
                    frontier.append(child["header"]["block_hash"])
    return state


def canonical_chain(state: RefState) -> list[str]:
    return [] if state.tip is None else list(reversed(state.ancestry(state.tip)))


def derived_view(state: RefState) -> dict:
    """沿最终权威链从创世到链尖累计；同一 tx_id 全局只产生一个贡献。"""

    balances: dict[str, int] = {}
    seen_tx: set[str] = set()
    contributing_blocks: set[str] = set()
    for block_hash in canonical_chain(state):
        for tx in state.blocks[block_hash].txs:
            if tx["tx_id"] in seen_tx:
                continue
            seen_tx.add(tx["tx_id"])
            contributing_blocks.add(block_hash)
            balances[tx["recipient"]] = balances.get(tx["recipient"], 0) + tx["amount"]
    return {
        "canonical_chain": canonical_chain(state),
        "tip_hash": state.tip,
        "balances": balances,
        "contribution_count": len(seen_tx),
        "contributing_blocks": sorted(contributing_blocks),
    }
