"""分块 posting 列表、跳跃游标与集合运算（算法核心，不依赖存储层）。

设计不变量（也是测试断言的重点）：

1. 每个 posting 列表是 **严格递增、无重复** 的文档 ID 序列，按固定块大小切分；
   每个块保存该块最大 ID（block upper bound）。
2. 块上界**只用于安全跳过**：advance_to(target) 仅当
   ``block_max < target`` 时整体跳过该块——被跳过的块不可能包含 >= target
   的 ID，因此跳过是安全的，绝不改变结果集合。
3. 删除文档不在这里处理：posting 列表是 term 对**已索引文档**的静态超集，
   删除只同步“版本化文档全集”的可见性，查询计划再与可见全集求交。
   因此删除无需重写任何块，块上界永远安全。
4. NOT/补集只接受调用方显式传入的有限 universe，见 :func:`difference`；
   本模块没有任何“无限整数补集”的入口。
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_BLOCK_SIZE = 8


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """一个定长 posting 块（最后一块可较短）。ids 严格递增，upper 为 ids[-1]。"""

    ids: tuple[int, ...]
    upper: int

    def __post_init__(self) -> None:
        if not self.ids:
            raise ValueError("空块不允许存在")
        if list(self.ids) != sorted(self.ids) or len(set(self.ids)) != len(self.ids):
            raise ValueError("块内 ids 必须严格递增且无重复")
        if self.upper != self.ids[-1]:
            raise ValueError("块上界必须等于块内最后一个 ID")


@dataclass(frozen=True)
class PostingList:
    """分块 posting 列表（内存表示；持久化编解码见 storage.encoding）。"""

    blocks: tuple[Block, ...]
    block_size: int = DEFAULT_BLOCK_SIZE

    def __post_init__(self) -> None:
        if self.block_size < 1:
            raise ValueError("block_size 必须 >= 1")
        # 跨块也必须严格递增（上一块 upper < 下一块首 ID）
        for b in self.blocks:
            if len(b.ids) > self.block_size:
                raise ValueError("块长度超过 block_size")
        for a, b in zip(self.blocks, self.blocks[1:]):
            if a.upper >= b.ids[0]:
                raise ValueError("块之间必须严格递增")

    # ---- 构造 ----

    @classmethod
    def from_sorted(cls, ids, block_size: int = DEFAULT_BLOCK_SIZE) -> "PostingList":
        """从严格递增、无重复的整数序列构造（重复/乱序直接报错，不静默去重）。"""
        normalized: list[int] = []
        prev = None
        for x in ids:
            if isinstance(x, bool) or not isinstance(x, int):
                raise TypeError(f"文档 ID 必须是 int，收到 {x!r}")
            if x < 0:
                raise ValueError(f"文档 ID 不能为负：{x}")
            if prev is not None and x <= prev:
                raise ValueError(
                    f"from_sorted 需要严格递增无重复序列：{prev} 后出现 {x}"
                )
            normalized.append(x)
            prev = x
        blocks: list[Block] = []
        for i in range(0, len(normalized), block_size):
            chunk = tuple(normalized[i : i + block_size])
            blocks.append(Block(chunk, chunk[-1]))
        return cls(tuple(blocks), block_size)

    @classmethod
    def from_blocks(cls, blocks, block_size: int) -> "PostingList":
        return cls(tuple(blocks), block_size)

    @classmethod
    def empty(cls, block_size: int = DEFAULT_BLOCK_SIZE) -> "PostingList":
        return cls((), block_size)

    # ---- 视图 ----

    @property
    def ids(self) -> tuple[int, ...]:
        out: list[int] = []
        for b in self.blocks:
            out.extend(b.ids)
        return tuple(out)

    def __len__(self) -> int:
        return sum(len(b.ids) for b in self.blocks)

    def __iter__(self):
        for b in self.blocks:
            yield from b.ids

    @property
    def block_uppers(self) -> tuple[int, ...]:
        return tuple(b.upper for b in self.blocks)


# ---------------------------------------------------------------------------
# 游标与执行统计
# ---------------------------------------------------------------------------


@dataclass
class CursorStats:
    """单个游标生命周期内的工作量计数。"""

    next_calls: int = 0
    advance_calls: int = 0
    doc_examined: int = 0  # next() 逐一遍历检查的文档数
    blocks_skipped: int = 0  # advance_to() 中整体跳过的块数
    docs_skipped_in_blocks: int = 0  # 被跳过块内的文档数（解释“省了多少”）


@dataclass
class Cursor:
    """posting 列表上的单向游标。

    位置用 (块索引 bi, 块内下标 bj) 表示；初始态 bi=bj=-1，
    耗尽态 bi == len(blocks)。
    """

    plist: PostingList
    stats: CursorStats = field(default_factory=CursorStats)
    _bi: int = -1
    _bj: int = -1

    @property
    def exhausted(self) -> bool:
        return self._bi >= len(self.plist.blocks)

    def current(self) -> int | None:
        if 0 <= self._bi < len(self.plist.blocks):
            return self.plist.blocks[self._bi].ids[self._bj]
        return None

    def next(self) -> int | None:
        """前进一步并返回当前 ID；耗尽返回 None。每个经过的 ID 计一次检查。"""
        self.stats.next_calls += 1
        if self.exhausted:
            return None
        if self._bi == -1:
            self._bi, self._bj = 0, 0
        else:
            self._bj += 1
            if self._bj >= len(self.plist.blocks[self._bi].ids):
                self._bi += 1
                self._bj = 0
        if self.exhausted:
            return None
        self.stats.doc_examined += 1
        return self.current()

    def advance_to(self, target: int) -> int | None:
        """前进到第一个 >= target 的 ID；没有则耗尽。

        安全跳过：只有 ``block.upper < target`` 的块可整体跳过。
        落在候选块内后仍逐 ID 检查（计入 doc_examined），不假设块内有序之外的东西。
        """
        self.stats.advance_calls += 1
        # 起点：当前块（初始态为第 0 块）
        if self._bi == -1:
            self._bi, self._bj = 0, 0
        # 1) 整块跳跃
        while self._bi < len(self.plist.blocks):
            blk = self.plist.blocks[self._bi]
            if blk.upper < target:
                self.stats.blocks_skipped += 1
                self.stats.docs_skipped_in_blocks += len(blk.ids)
                self._bi += 1
                self._bj = 0
                continue
            break
        if self._bi >= len(self.plist.blocks):
            return None
        # 2) 块内顺序推进到 >= target
        blk = self.plist.blocks[self._bi]
        while self._bj < len(blk.ids):
            cur = blk.ids[self._bj]
            self.stats.doc_examined += 1
            if cur >= target:
                return cur
            self._bj += 1
        # 当前块耗尽（理论上 upper >= target 时不会发生，防御性进入下一块）
        self._bi += 1
        self._bj = 0
        if self._bi < len(self.plist.blocks):
            return self.advance_to(target)
        return None


# ---------------------------------------------------------------------------
# 集合操作统计与实现
# ---------------------------------------------------------------------------


@dataclass
class OpStats:
    """一次集合操作的合并统计。"""

    comparisons: int = 0
    doc_examined: int = 0
    blocks_skipped: int = 0
    docs_skipped_in_blocks: int = 0
    next_calls: int = 0
    advance_calls: int = 0

    @classmethod
    def from_cursors(cls, cursors, comparisons: int = 0) -> "OpStats":
        s = cls(comparisons=comparisons)
        for c in cursors:
            s.doc_examined += c.stats.doc_examined
            s.blocks_skipped += c.stats.blocks_skipped
            s.docs_skipped_in_blocks += c.stats.docs_skipped_in_blocks
            s.next_calls += c.stats.next_calls
            s.advance_calls += c.stats.advance_calls
        return s


def _new_cursors(plists) -> list[Cursor]:
    return [Cursor(p) for p in plists]


def intersect(a: PostingList, b: PostingList) -> tuple[PostingList, OpStats]:
    """两个 posting 列表求交。

    以 a 为驱动逐条 next()，用 b.advance_to() 跳跃；每次 advance 只跳过
    upper < 当前驱动值的块。块大小不一致也安全（只依赖块上界语义）。
    """
    ca, cb = Cursor(a), Cursor(b)
    out: list[int] = []
    comparisons = 0
    x = ca.next()
    y = cb.next()
    while x is not None and y is not None:
        comparisons += 1
        if x == y:
            out.append(x)  # 两列表各自唯一 => 结果天然唯一
            x = ca.next()
            y = cb.next()
        elif x < y:
            x = ca.next()
        else:
            y = cb.advance_to(x)
    stats = OpStats.from_cursors([ca, cb], comparisons)
    return PostingList.from_sorted(out, a.block_size), stats


def union(a: PostingList, b: PostingList) -> tuple[PostingList, OpStats]:
    """归并求并；相遇时只收一次，保证每个结果 ID 唯一。"""
    ca, cb = Cursor(a), Cursor(b)
    out: list[int] = []
    comparisons = 0
    x = ca.next()
    y = cb.next()
    while x is not None or y is not None:
        comparisons += 1
        if y is None or (x is not None and x < y):
            out.append(x)
            x = ca.next()
        elif x is None or y < x:
            out.append(y)
            y = cb.next()
        else:  # x == y：只收一次
            out.append(x)
            x = ca.next()
            y = cb.next()
    stats = OpStats.from_cursors([ca, cb], comparisons)
    return PostingList.from_sorted(out, a.block_size), stats


def difference(
    universe: PostingList, other: PostingList
) -> tuple[PostingList, OpStats]:
    """universe - other。

    universe 必须由调用方显式给出（版本化的可见文档全集），
    本函数绝不假设“非 other 即结果”——那会把 NOT 补成无限整数集。
    """
    cu, co = Cursor(universe), Cursor(other)
    out: list[int] = []
    comparisons = 0
    u = cu.next()
    o = co.next()
    while u is not None:
        comparisons += 1
        if o is None:
            out.append(u)
            u = cu.next()
        elif u == o:
            u = cu.next()
            o = co.next()
        elif u < o:
            out.append(u)
            u = cu.next()
        else:  # o < u：o 中更靠前的元素与 universe 无关，跳它
            o = co.advance_to(u)
    stats = OpStats.from_cursors([cu, co], comparisons)
    return PostingList.from_sorted(out, universe.block_size), stats
