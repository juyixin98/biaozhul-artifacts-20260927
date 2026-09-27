"""算法索引核心测试：分块、游标安全跳跃、集合代数与唯一性不变量。"""
from __future__ import annotations

import itertools

import pytest

from app.index.posting import (
    Block,
    Cursor,
    PostingList,
    difference,
    intersect,
    union,
)

BS = 4  # 测试用小块，更容易触发跨块跳跃


def pl(ids, block_size=BS):
    return PostingList.from_sorted(ids, block_size)


# ---------------------------------------------------------------------------
# 分块与构造不变量
# ---------------------------------------------------------------------------


def test_from_sorted_partitions_into_blocks_with_uppers():
    p = pl([1, 3, 5, 7, 10, 12])
    assert [b.upper for b in p.blocks] == [7, 12]
    assert [tuple(b.ids) for b in p.blocks] == [(1, 3, 5, 7), (10, 12)]
    assert p.block_uppers == (7, 12)
    assert p.ids == (1, 3, 5, 7, 10, 12)


def test_from_sorted_rejects_duplicates_and_unsorted():
    with pytest.raises(ValueError, match="严格递增"):
        pl([1, 2, 2])
    with pytest.raises(ValueError, match="严格递增"):
        pl([3, 1])
    with pytest.raises(TypeError):
        pl([1, True])  # bool 不是合法 ID
    with pytest.raises(ValueError):
        pl([1, -2])


def test_block_upper_must_be_last_id():
    with pytest.raises(ValueError):
        Block((1, 2, 3), upper=99)


def test_empty_list_has_no_blocks_and_is_iterable():
    p = pl([])
    assert p.blocks == ()
    assert list(p) == []
    assert len(p) == 0


# ---------------------------------------------------------------------------
# 游标
# ---------------------------------------------------------------------------


def test_cursor_next_visits_in_order_then_none():
    c = Cursor(pl([1, 2, 3, 4, 5]))
    seen = []
    while True:
        v = c.next()
        if v is None:
            break
        seen.append(v)
    assert seen == [1, 2, 3, 4, 5]
    # 耗尽后反复 next 仍为 None，不抛异常
    assert c.next() is None


def test_advance_to_returns_first_ge_target():
    c = Cursor(pl([1, 3, 5, 7, 10, 12, 15]))
    assert c.advance_to(6) == 7
    assert c.advance_to(12) == 12  # 等于目标
    assert c.advance_to(100) is None  # 超界耗尽


def test_advance_to_only_skips_blocks_whose_upper_below_target():
    p = pl([1, 3, 5, 7, 10, 12, 15, 17, 20])
    c = Cursor(p)
    v = c.advance_to(11)
    assert v == 12
    # 块 [1,3,5,7](upper=7) 可安全跳过；块 [10,12,15,17](upper=17) 不可跳
    assert c.stats.blocks_skipped == 1
    assert c.stats.docs_skipped_in_blocks == 4


def test_advance_to_skips_multiple_blocks():
    p = pl(list(range(0, 40, 2)))  # 10 个块，每块 4 个
    c = Cursor(p)
    assert c.advance_to(31) == 32
    # upper <31 的整块都应跳过（upper 为 6,14,22,30 的 4 块）
    assert c.stats.blocks_skipped == 4


def test_advance_to_never_skips_block_that_could_contain_target():
    """安全跳跃的关键：upper == target-1 可跳，upper == target 不可跳。"""
    p = pl([1, 2, 3, 4, 5, 6, 7, 8, 9])
    c = Cursor(p)
    assert c.advance_to(8) == 8
    assert c.stats.blocks_skipped == 1  # 只跳 upper=4 的块
    # 第二块 upper=8 == target，必须进入块内检查


# ---------------------------------------------------------------------------
# 集合代数（与朴素集合实现逐一对照）
# ---------------------------------------------------------------------------


def _set(p: PostingList) -> set[int]:
    return set(p.ids)


def test_intersect_matches_set_algebra_and_uses_skips():
    a = pl(list(range(0, 100, 3)))
    b = pl(list(range(0, 100, 7)))
    got, stats = intersect(a, b)
    assert _set(got) == _set(a) & _set(b)
    assert list(got.ids) == sorted(set(got.ids))  # 唯一且有序
    # 稀疏驱动 + 跳跃：至少应跳过若干块
    assert stats.blocks_skipped >= 1


def test_union_matches_set_algebra_and_dedups():
    a = pl([1, 2, 4, 8])
    b = pl([2, 3, 8, 9])
    got, stats = union(a, b)
    assert list(got.ids) == [1, 2, 3, 4, 8, 9]
    assert _set(got) == _set(a) | _set(b)


def test_difference_requires_explicit_universe_and_matches_set_algebra():
    universe = pl([1, 2, 3, 4, 5, 6])
    other = pl([2, 4, 6])
    got, stats = difference(universe, other)
    assert list(got.ids) == [1, 3, 5]
    assert _set(got) == _set(universe) - _set(other)
    # NOT 语义：补集被 universe 限定，不会冒出 universe 之外的任何整数
    assert max(got.ids, default=0) <= 6


def test_difference_with_other_containing_ids_outside_universe():
    universe = pl([10, 20, 30])
    other = pl([1, 20, 99])
    got, _ = difference(universe, other)
    assert list(got.ids) == [10, 30]  # 1、99 绝不出现在补集里


def test_intersect_disjoint_is_empty_with_full_scan_safety():
    a = pl([1, 2])
    b = pl([100, 200])
    got, stats = intersect(a, b)
    assert list(got.ids) == []
    assert stats.comparisons >= 2


# ---------------------------------------------------------------------------
# 朴素参考对比（小空间穷举：不同块大小结果必须一致）
# ---------------------------------------------------------------------------


def test_fuzz_against_python_sets_many_shapes():
    import random

    rng = random.Random(20260928)
    for _ in range(300):
        universe = set(range(rng.randint(0, 60)))
        a = set(rng.sample(sorted(universe), rng.randint(0, len(universe))))
        b = set(rng.sample(sorted(universe), rng.randint(0, len(universe))))
        bs = rng.choice([1, 2, 3, 5, 8, 16])
        pa, pb = pl(sorted(a), bs), pl(sorted(b), bs)
        assert _set(intersect(pa, pb)[0]) == a & b
        assert _set(union(pa, pb)[0]) == a | b
        assert _set(difference(pa, pb)[0]) == a - b
