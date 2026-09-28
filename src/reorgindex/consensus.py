"""固定的共识规则（纯函数，无存储依赖）。

权重模型（合成链，刻意简单且固定）：
- 每个区块带正整数 ``weight``；一条链的累计权重 = 沿父哈希回到分叉点
  之间区块权重之和。
- 比较候选链与当前权威链时只看分叉点之后的两段累计权重（共同祖先之前的
  权重对双方相同，比较时抵消）。
- 严格更大才切换；权重相等时保留当前权威链（tie_keep_canonical）。

最终性模型：
- 深度 D（fin_confirmed 的判定为 ``tip_height - block_height >= D``。
- 重组允许撤掉的最深区间长度 = D：回滚区块数 > D 时，必然要撤掉一个
  已最终确定区块，内核必须以 FinalityReorgError 明确拒绝。
"""

from __future__ import annotations

from .errors import FinalityReorgError


def is_finalized(block_height: int, tip_height: int, finality_depth: int) -> bool:
    return tip_height - block_height >= finality_depth


def confirmations(block_height: int, tip_height: int) -> int:
    """确认深度：该块之后（含该块）到链尖的块数，tip 自身为 1。"""

    return tip_height - block_height + 1


def is_reorg_allowed(rollback_count: int, finality_depth: int) -> bool:
    """回滚数量在最终性边界以内才允许重组。

    旧链尖高度 h、回滚 r 个区块时，被撤掉的最老一块高度为 h-r+1，它在回滚前
    的确认数为 r。已最终确定要求 h - block_height >= D，即 r-1 >= D、r >= D+1。
    因此 r <= D 安全；r == D+1 必然撤掉高度 h-D 的已最终确定区块，拒绝。
    """

    return rollback_count <= finality_depth


def ensure_reorg_allowed(rollback_count: int, finality_depth: int, **context) -> None:
    if not is_reorg_allowed(rollback_count, finality_depth):
        raise FinalityReorgError(
            f"重组需回滚 {rollback_count} 个区块，超过最终性深度 {finality_depth}，拒绝",
            rollback_count=rollback_count,
            finality_depth=finality_depth,
            **context,
        )


def should_switch(
    candidate_weight: int,
    incumbent_weight: int,
    tie_keep_canonical: bool = True,
) -> bool:
    """候选段累计权重严格更大才切换；相等不切换。"""

    if candidate_weight > incumbent_weight:
        return True
    if candidate_weight < incumbent_weight:
        return False
    return not tie_keep_canonical
