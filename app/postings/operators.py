"""AND / OR / NOT 的核心合并算子（算法索引的真正工作所在）。

这些函数只依赖 BlockedPostingList + Cursor，不查询数据库；
统计信息（跳过块数、逐 ID 步数、比较次数）随结果一起返回。

输出列表同样经过 from_ids（排序去重）构造，保证“每个结果 ID 唯一”。
"""
from __future__ import annotations

from typing import List, Tuple

from .blocked_list import BlockedPostingList, Cursor, OpStats


def _new_cursors(a: BlockedPostingList, b: BlockedPostingList) -> Tuple[Cursor, Cursor]:
    return a.cursor(), b.cursor()


def intersect(a: BlockedPostingList, b: BlockedPostingList) -> Tuple[BlockedPostingList, OpStats]:
    """交集 A ∩ B：zig-zag（galloping）合并。

    每轮让当前值较小的一侧 skip_to(另一侧当前值)：
    块上界允许时整块跳过，否则块内逐步前进。两侧相等时输出并各进一步。
    """
    stats = OpStats()
    ca, cb = _new_cursors(a, b)
    out: List[int] = []

    while ca.valid() and cb.valid():
        va, vb = ca.value(), cb.value()
        if va == vb:
            out.append(va)
            ca.step()
            cb.step()
        elif va < vb:
            ca.skip_to(vb)
        else:
            cb.skip_to(va)

    stats.add_cursor(ca)
    stats.add_cursor(cb)
    result = BlockedPostingList.from_ids(f"({a.term}) AND ({b.term})", out, a.block_size)
    stats.results_emitted = result.length
    return result, stats


def union(a: BlockedPostingList, b: BlockedPostingList) -> Tuple[BlockedPostingList, OpStats]:
    """并集 A ∪ B：两路归并；相等时只输出一次（结果 ID 唯一）。"""
    stats = OpStats()
    ca, cb = _new_cursors(a, b)
    out: List[int] = []

    while ca.valid() and cb.valid():
        va, vb = ca.value(), cb.value()
        if va == vb:
            out.append(va)
            ca.step()
            cb.step()
        elif va < vb:
            out.append(va)
            ca.step()
        else:
            out.append(vb)
            cb.step()

    tail = ca if ca.valid() else cb
    while tail.valid():
        out.append(tail.value())
        tail.step()

    stats.add_cursor(ca)
    stats.add_cursor(cb)
    result = BlockedPostingList.from_ids(f"({a.term}) OR ({b.term})", out, a.block_size)
    stats.results_emitted = result.length
    return result, stats


def intersect_scan(
    lead: BlockedPostingList, follow: BlockedPostingList
) -> Tuple[BlockedPostingList, OpStats]:
    """非对称求交：逐个扫描 lead，follow 侧用 skip_to 跳跃追赶。

    与对称的 intersect 结果完全相同，但跳过块统计依赖“谁当 lead”，
    用于验证不同执行顺序结果一致而统计不同。
    """
    stats = OpStats()
    clead = lead.cursor()
    cfollow = follow.cursor()
    out: List[int] = []

    while clead.valid():
        v = clead.value()
        cfollow.skip_to(v)
        if cfollow.valid() and cfollow.value() == v:
            out.append(v)
        clead.step()

    stats.add_cursor(clead)
    stats.add_cursor(cfollow)
    result = BlockedPostingList.from_ids(
        f"({lead.term}) SCAN-AND ({follow.term})", out, lead.block_size
    )
    stats.results_emitted = result.length
    return result, stats


def difference(base: BlockedPostingList, subtract: BlockedPostingList) -> Tuple[BlockedPostingList, OpStats]:
    """差集 base ＼ subtract（NOT 的核心操作）。

    顺序扫描 base；subtract 侧用 skip_to 跳跃追赶。相等则该 ID 被排除，
    否则输出。base 本身严格递增，故输出天然唯一。
    """
    stats = OpStats()
    cbase = base.cursor()
    csub = subtract.cursor()
    out: List[int] = []

    while cbase.valid():
        v = cbase.value()
        csub.skip_to(v)
        if csub.valid() and csub.value() == v:
            cbase.step()  # 被减去：不输出
        else:
            out.append(v)
            cbase.step()

    stats.add_cursor(cbase)
    stats.add_cursor(csub)
    result = BlockedPostingList.from_ids(
        f"({base.term}) NOT ({subtract.term})", out, base.block_size
    )
    stats.results_emitted = result.length
    return result, stats
