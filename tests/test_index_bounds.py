"""剪枝下界测试：用穷举对拍证明下界永不超过真实距离（不漏阈值内候选）。"""
from __future__ import annotations

import random

import pytest

from app.editdistance import distance
from app.index import (
    LexiconIndex,
    combined_lower_bound,
    lower_bound_counts,
    lower_bound_length,
)

from .oracle import shortest_path


@pytest.mark.parametrize("seed", list(range(120)))
def test_lower_bounds_never_exceed_true_distance(unit_profile, seed):
    rng = random.Random(3000 + seed)
    alpha = rng.choice(["ab", "abc", "abcd", "abxy"])
    q = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 6)))
    c = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 6)))
    # 真实距离来自被测 DP；DP 本身在 test_editdistance 中已与独立 oracle
    # 对拍（短串枚举 + 随机），这里只检验下界的可接纳性。
    true_d = distance(q, c, unit_profile)
    lb_len = lower_bound_length(q, c, unit_profile)
    lb_cnt = lower_bound_counts(q, c, unit_profile)
    lb = combined_lower_bound(q, c, unit_profile)
    assert lb_len <= true_d + 1e-9, (q, c, lb_len, true_d)
    assert lb_cnt <= true_d + 1e-9, (q, c, lb_cnt, true_d)
    assert lb <= true_d + 1e-9, (q, c, lb, true_d)
    assert lb == max(lb_len, lb_cnt)


@pytest.mark.parametrize("seed", list(range(60)))
def test_lower_bounds_admissible_weighted(weighted_profile, seed):
    rng = random.Random(4000 + seed)
    alpha = "abc0ox"
    q = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 6)))
    c = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 6)))
    true_d = distance(q, c, weighted_profile)
    lb = combined_lower_bound(q, c, weighted_profile)
    assert lb <= true_d + 1e-9, (q, c, lb, true_d)


def test_pruning_keeps_every_within_threshold_candidate(unit_profile):
    words = ["abc", "abd", "ab", "abcd", "xyz", "bac", "acb", "a", "qqqqq"]
    index = LexiconIndex("v", [(w, 1) for w in words])
    query = "abc"
    for threshold in [0.0, 0.5, 1.0, 1.5, 2.0, 3.0]:
        survivors = [
            e.word for e in index.length_bucket_candidates(
                len(query), threshold, unit_profile.min_indel_cost())
            if combined_lower_bound(query, e.word, unit_profile) <= threshold + 1e-9
        ]
        for w in words:
            d = distance(query, w, unit_profile)
            if d <= threshold + 1e-9:
                assert w in survivors, (
                    f"阈值 {threshold} 内候选 {w}（距离 {d}）被剪枝漏掉"
                )


def test_length_bound_basic(unit_profile):
    assert lower_bound_length("ab", "abcde", unit_profile) == 3.0
    assert lower_bound_length("abcde", "ab", unit_profile) == 3.0


def test_count_bound_matches_known_values(unit_profile):
    # anagram（含一次交换）：计数相同，下界 0（下界宽松但可接纳）
    assert lower_bound_counts("abc", "bac", unit_profile) == 0.0
    # 完全不相交字符、等长：Δ=6，3 对 * min(2, 1) = 3
    assert lower_bound_counts("abc", "xyz", unit_profile) == pytest.approx(3.0)
    # 多出 2 个字符：Δ=2，1 对不可配对消化（只有一种字符），实际是
    # leftover=0 + pair 按 min(2*1, sub=1)=1 —— 但 aa->'' 不能替换。
    # 下界允许低估（只要求可接纳），断言 <= 真实距离 2。
    assert lower_bound_counts("", "aa", unit_profile) <= 2.0 + 1e-9


def test_index_buckets_stable_order():
    words = ["bb", "aa", "ccc", "a", "b"]
    index = LexiconIndex("v", [(w, 1) for w in words])
    assert [e.word for e in index.all_entries()] == ["a", "aa", "b", "bb", "ccc"]
