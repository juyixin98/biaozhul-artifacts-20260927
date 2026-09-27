"""分块（blocked）posting 列表与跳跃游标。

不变量
------
1. posting 是排序后、去重的整数文档 ID 列表（严格递增）。
2. 列表被切成大小为 block_size 的块；每块记录其上界（块内最大 ID）。
3. 跳跃块上界**只用于安全跳过**：只有当某块的上界仍小于目标值时，
   才允许整块跳过；若目标可能落在块内，必须进入块内逐个核对。
   因此跳过不会越过任何满足条件的 ID（skip 的安全性由该规则保证）。
4. 游标暴露块级/比较级计数器，由上层汇总为执行统计；
   核心求交/并集/差集完全在本模块的 Python 合并算法中完成，
   不使用任何数据库集合查询代替。
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple


class PostingInvariantError(AssertionError):
    """posting 列表破坏了“严格递增、去重”的不变量。"""


def _check_sorted_unique(ids: Sequence[int]) -> None:
    last = None
    for x in ids:
        if last is not None and x <= last:
            raise PostingInvariantError(f"posting 必须严格递增且唯一，发现 {x} 排在 {last} 之后")
        last = x


@dataclass(frozen=True)
class BlockSpan:
    start: int  # 块在 doc_ids 中的起始下标（含）
    end: int  # 块在 doc_ids 中的结束下标（不含）

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class BlockedPostingList:
    """单个词项在某个版本上的 posting 列表（内存表示）。"""

    term: str
    doc_ids: Tuple[int, ...]
    block_size: int = 8
    blocks: Tuple[BlockSpan, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.block_size < 1:
            raise ValueError("block_size 必须 >= 1")
        _check_sorted_unique(self.doc_ids)
        object.__setattr__(self, "doc_ids", tuple(self.doc_ids))
        blocks: List[BlockSpan] = []
        for start in range(0, len(self.doc_ids), self.block_size):
            blocks.append(BlockSpan(start=start, end=min(start + self.block_size, len(self.doc_ids))))
        object.__setattr__(self, "blocks", tuple(blocks))

    @property
    def length(self) -> int:
        return len(self.doc_ids)

    def block_upper_bound(self, bi: int) -> int:
        """第 bi 块的安全跳跃上界 = 块内最大 ID。"""
        blk = self.blocks[bi]
        return self.doc_ids[blk.end - 1]

    def cursor(self) -> "Cursor":
        return Cursor(self)

    @staticmethod
    def from_ids(term: str, ids: Sequence[int], block_size: int = 8) -> "BlockedPostingList":
        uniq_sorted = sorted(set(ids))
        return BlockedPostingList(term=term, doc_ids=tuple(uniq_sorted), block_size=block_size)

    @staticmethod
    def empty(term: str = "<empty>", block_size: int = 8) -> "BlockedPostingList":
        return BlockedPostingList(term=term, doc_ids=(), block_size=block_size)


@dataclass
class CursorStats:
    blocks_skipped: int = 0  # 因上界 < 目标而整体跳过的块数
    ids_stepped: int = 0  # 块内逐个前进的 ID 步数
    comparisons: int = 0  # 与目标值之间的比较次数
    block_probes: int = 0  # 查询块上界的次数

    def as_dict(self) -> dict:
        return {
            "blocks_skipped": self.blocks_skipped,
            "ids_stepped": self.ids_stepped,
            "comparisons": self.comparisons,
            "block_probes": self.block_probes,
        }


class Cursor:
    """在 BlockedPostingList 上单向移动的游标。

    当前位置由索引 idx 给出；valid() 为真时 value() 为当前 ID，
    否则表示已到列表末尾（DONE）。skip_to(target) 把游标移动到
    “第一个 >= target”的 ID（或末尾）。
    """

    DONE: int = -1

    def __init__(self, plist: BlockedPostingList):
        self.plist = plist
        self.idx = 0
        self.stats = CursorStats()

    # ---- 基本访问 ----
    def valid(self) -> bool:
        return self.idx < len(self.plist.doc_ids)

    def value(self) -> int:
        return self.plist.doc_ids[self.idx]

    def current_block_index(self) -> Optional[int]:
        """当前 ID 所属块；列表为空或游标越界时返回 None。"""
        if not self.valid():
            return None
        # 块大小固定（最后一块可能较短），整除即可。
        return self.idx // self.plist.block_size

    # ---- 移动 ----
    def step(self) -> None:
        """块内前进一步（统计为逐 ID 步数，不统计为跳块）。"""
        if self.valid():
            self.idx += 1
            self.stats.ids_stepped += 1

    def skip_to(self, target: int) -> None:
        """安全跳跃到第一个 >= target 的位置。

        跳跃策略：
        - 先用当前块的上界判断：若整个当前块都 < target，则顺序检查
          后续块的上界，整块跳过（每跳过一块记 blocks_skipped += 1）。
        - 落在可能包含 target 的块之后，块内顺序前进到 >= target。

        “上界 < target 才跳过”保证不会越过目标，故不会跳过任何
        本应命中的 ID。
        """
        if not self.valid():
            return

        # 阶段 1：块级安全跳过。
        bi = self.current_block_index()
        while bi is not None:
            self.stats.block_probes += 1
            self.stats.comparisons += 1
            upper = self.plist.block_upper_bound(bi)
            if upper < target:
                blk = self.plist.blocks[bi]
                self.idx = blk.end  # 整块越过
                self.stats.blocks_skipped += 1
                bi = self.current_block_index()
                continue
            break  # upper >= target：目标可能在本块，必须进入块内

        # 阶段 2：块内顺序定位。
        while self.valid():
            self.stats.comparisons += 1
            if self.value() >= target:
                return
            self.step()


@dataclass
class OpStats:
    """一次合并算子（或一个 AST 节点）的执行统计。"""

    blocks_skipped: int = 0
    ids_stepped: int = 0
    comparisons: int = 0
    block_probes: int = 0
    results_emitted: int = 0

    def add_cursor(self, cur: Cursor) -> None:
        self.blocks_skipped += cur.stats.blocks_skipped
        self.ids_stepped += cur.stats.ids_stepped
        self.comparisons += cur.stats.comparisons
        self.block_probes += cur.stats.block_probes

    def merge(self, other: "OpStats") -> "OpStats":
        # 工作量计数器（跳过块/步数/比较/探块）跨节点累加；
        # results_emitted 是“本节点输出量”，不做跨节点求和，
        # 由各求值节点按自身最终结果显式设置。
        return OpStats(
            blocks_skipped=self.blocks_skipped + other.blocks_skipped,
            ids_stepped=self.ids_stepped + other.ids_stepped,
            comparisons=self.comparisons + other.comparisons,
            block_probes=self.block_probes + other.block_probes,
            results_emitted=0,
        )

    def as_dict(self) -> dict:
        return {
            "blocks_skipped": self.blocks_skipped,
            "ids_stepped": self.ids_stepped,
            "comparisons": self.comparisons,
            "block_probes": self.block_probes,
            "results_emitted": self.results_emitted,
        }


# 二分定位的比较次数可被测试用于核对“上界只用于安全跳过”，
# 这里保留一个独立小工具（合并算子不依赖它做结果正确性）。
def lower_bound_comparisons(sorted_ids: Sequence[int], target: int) -> Tuple[int, int]:
    """手写二分：返回 (第一个 >= target 的下标, 比较次数)。"""
    lo, hi = 0, len(sorted_ids)
    comps = 0
    while lo < hi:
        mid = (lo + hi) // 2
        comps += 1
        if sorted_ids[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo, comps


def bisect_position(sorted_ids: Sequence[int], target: int) -> int:
    """标准库 bisect 封装，供需要下标但不计统计的场合使用。"""
    return bisect_left(sorted_ids, target)
