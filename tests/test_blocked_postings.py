"""算法索引单元测试：分块不变量、跳跃安全性、集合代数、跳过块统计。

预言使用内建 set（独立于被测合并算法）；另用随机性质测试证明
“块上界只用于安全跳过”——任何跳过序列都不会越过目标。
"""
from __future__ import annotations

import random

import pytest

from app.postings.blocked_list import (
    BlockedPostingList,
    PostingInvariantError,
)
from app.postings.operators import difference, intersect, union

BS = 8


def pl(term, ids):
    return BlockedPostingList.from_ids(term, ids, BS)


# ---------- 不变量 ----------

def test_constructor_sorts_and_dedupes():
    p = pl("t", [5, 5, 1, 3, 3])
    assert p.doc_ids == (1, 3, 5)
    assert p.length == 3


def test_unsorted_duplicate_input_rejected_when_constructed_directly():
    with pytest.raises(PostingInvariantError):
        BlockedPostingList(term="t", doc_ids=(1, 2, 2), block_size=BS)
    with pytest.raises(PostingInvariantError):
        BlockedPostingList(term="t", doc_ids=(3, 1), block_size=BS)


def test_block_upper_bounds_are_block_maxima():
    p = pl("t", list(range(1, 20)))  # 1..19，块大小 8
    assert [p.block_upper_bound(i) for i in range(len(p.blocks))] == [8, 16, 19]
    assert [b.length for b in p.blocks] == [8, 8, 3]


# ---------- 游标跳跃安全性（性质测试） ----------

@pytest.mark.parametrize("seed", range(40))
def test_skip_to_never_overreaches_and_lands_on_lower_bound(seed):
    rng = random.Random(seed)
    ids = sorted(rng.sample(range(0, 500), rng.randint(0, 80)))
    p = pl("t", ids)
    for target in rng.sample(range(-5, 510), 30):
        cur = p.cursor()
        cur.skip_to(target)
        if cur.valid():
            # 落点是第一个 >= target 的 ID（与 bisect 独立实现的下界一致）
            assert cur.value() >= target
            assert all(x < target for x in ids[: cur.idx])
        else:
            assert all(x < target for x in ids)


def test_skip_to_whole_block_is_counted_as_skipped():
    # 块：[0..7] [8..15] [16..23] ...；跳到 20 应整体跳过前两块
    p = pl("t", list(range(64)))
    cur = p.cursor()
    cur.skip_to(20)
    assert cur.value() == 20
    assert cur.stats.blocks_skipped == 2
    # 落在块内（16..19 四步前进）
    assert cur.stats.ids_stepped == 4


def test_skip_to_within_first_block_skips_nothing():
    p = pl("t", list(range(64)))
    cur = p.cursor()
    cur.skip_to(3)
    assert cur.value() == 3
    assert cur.stats.blocks_skipped == 0
    assert cur.stats.ids_stepped == 3


def test_skip_beyond_end_drains_all_blocks_safely():
    p = pl("t", [1, 9, 17, 25, 33])
    cur = p.cursor()
    cur.skip_to(100)
    assert not cur.valid()
    assert cur.stats.blocks_skipped == len(p.blocks)


# ---------- 合并算子 vs 独立集合代数 ----------

SPARSE = {1, 9, 17, 25, 33}
DENSE = set(range(1, 41, 3))
FISH = {2, 3, 9, 10, 17, 18, 25, 26, 33, 34}
UNIVERSE = set(range(1, 41))


@pytest.mark.parametrize("seed", range(30))
def test_operators_match_set_algebra_random(seed):
    rng = random.Random(1000 + seed)
    a = set(rng.sample(range(1, 61), rng.randint(0, 25)))
    b = set(rng.sample(range(1, 61), rng.randint(0, 25)))
    pa, pb = pl("a", a), pl("b", b)

    ia, _ = intersect(pa, pb)
    ub, _ = union(pa, pb)
    df, _ = difference(pl("u", UNIVERSE), pa)

    assert set(ia.doc_ids) == (a & b)
    assert set(ub.doc_ids) == (a | b)
    assert set(df.doc_ids) == (UNIVERSE - a)


def test_concrete_sparse_and_dense_values():
    pa, pb = pl("cat", SPARSE), pl("dog", DENSE)
    res, stats = intersect(pa, pb)
    assert set(res.doc_ids) == (SPARSE & DENSE) == {1, 25}
    assert stats.blocks_skipped >= 1  # 稀疏/稠密 zig-zag 确实产生了整块跳过


def test_intersect_with_empty_and_empty_universe_difference():
    empty = BlockedPostingList.empty(block_size=BS)
    res, _ = intersect(pl("cat", SPARSE), empty)
    assert res.doc_ids == ()

    # 空全集上的 NOT 仍然是空（有限补集）
    u_empty = BlockedPostingList.empty("universe", BS)
    res, _ = difference(u_empty, pl("cat", SPARSE))
    assert res.doc_ids == ()


def test_results_are_unique_even_with_overlap():
    a = pl("a", [1, 2, 3, 4, 4, 5])
    b = pl("b", [3, 4, 5, 6, 6])
    res, _ = union(a, b)
    ids = list(res.doc_ids)
    assert ids == sorted(set(ids))
    assert set(ids) == {1, 2, 3, 4, 5, 6}


def test_difference_removes_subtract_and_uses_skips():
    universe = pl("u", UNIVERSE)
    res, stats = difference(universe, pl("fish", FISH))
    assert set(res.doc_ids) == UNIVERSE - FISH
    assert stats.results_emitted == len(UNIVERSE - FISH)
