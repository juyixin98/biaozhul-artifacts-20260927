"""算法核心测试：Damerau 变体、已知距离、路径重放/重算、与独立 oracle 对拍。"""
from __future__ import annotations

import random

import pytest

from app.costs import cost_profile_from_dict
from app.editdistance import EPS, distance, edit, replay, rescore_moves

from .oracle import assert_path_valid, shortest_path


# ---------- 手工可核验的具体结果 ----------

def test_identical_strings_zero(unit_profile):
    assert distance("abc", "abc", unit_profile) == 0.0


def test_empty_strings(unit_profile):
    assert distance("", "", unit_profile) == 0.0
    assert distance("", "abc", unit_profile) == 3.0
    assert distance("abc", "", unit_profile) == 3.0


def test_basic_indel_and_sub(unit_profile):
    assert distance("kitten", "sitting", unit_profile) == 3.0
    assert distance("saturday", "sunday", unit_profile) == 3.0
    assert distance("flaw", "lawn", unit_profile) == 2.0


def test_single_adjacent_transposition(unit_profile):
    assert distance("ab", "ba", unit_profile) == 1.0
    assert distance("hello", "hlelo", unit_profile) == 1.0


# ---------- 明确变体：非限制性 DL 必须区别于限制性 OSA ----------

def restricted_osa_distance(source: str, target: str, profile) -> float:
    """限制性最优串对齐（OSA）递推——仅作对照，断言我们的实现**不是**它。"""
    n, m = len(source), len(target)
    d = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i * profile.delete
    for j in range(m + 1):
        d[0][j] = j * profile.insert
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0.0 if source[i - 1] == target[j - 1] else profile.sub_cost(
                source[i - 1], target[j - 1])
            best = min(
                d[i - 1][j - 1] + cost,
                d[i - 1][j] + profile.delete,
                d[i][j - 1] + profile.insert,
            )
            if (i > 1 and j > 1
                    and source[i - 1] == target[j - 2]
                    and source[i - 2] == target[j - 1]):
                best = min(best, d[i - 2][j - 2] + profile.transpose)
            d[i][j] = best
    return d[n][m]


def test_unrestricted_beats_osa_on_ca_abc(unit_profile):
    # 经典区分例：CA -> ABC
    # 非限制性 DL = 2（交换 C,A -> A,C，再插入 B）
    # 限制性 OSA = 3（不允许对同一子串多次编辑）
    assert distance("ca", "abc", unit_profile) == pytest.approx(2.0)
    assert restricted_osa_distance("ca", "abc", unit_profile) == pytest.approx(3.0)


def test_unrestricted_never_exceeds_osa_and_divergence_exists(unit_profile):
    # 枚举短串：非限制性 DL 必须 <= OSA；且必须真实存在二者不同的串对，
    # 否则说明实现退化成了限制性递推。不同串对逐一与独立 oracle 对拍。
    from itertools import product

    alphabet = "abc"
    strings = ["".join(p) for r in range(0, 5) for p in product(alphabet, repeat=r)]
    divergences = []
    for s in strings:
        for t in strings:
            dl = distance(s, t, unit_profile)
            osa = restricted_osa_distance(s, t, unit_profile)
            assert dl <= osa + 1e-9, (s, t, dl, osa)
            if osa > dl + 1e-9:
                divergences.append((s, t, dl, osa))
    pairs = [(s, t) for s, t, _dl, _osa in divergences]
    assert ("ca", "abc") in pairs
    assert len(divergences) >= 5
    for s, t, dl, osa in divergences:
        od, op = shortest_path(s, t, unit_profile)
        assert dl == pytest.approx(od, abs=1e-9), (s, t, dl, od)
        assert_path_valid(s, t, op, unit_profile, od)


# ---------- 重复字符 ----------

@pytest.mark.parametrize("s,t", [
    ("aaa", "aaaa"),       # 纯插入
    ("aaaa", "aaa"),       # 纯删除
    ("aab", "aba"),        # 重复字符间的交换
    ("abba", "baba"),      # 重复+交换链
    ("aaa", "bbb"),        # 全替换（vs 删除+插入的取舍）
])
def test_repeated_chars_against_oracle(unit_profile, s, t):
    got = distance(s, t, unit_profile)
    oracle_dist, oracle_path = shortest_path(s, t, unit_profile)
    assert got == pytest.approx(oracle_dist, abs=1e-9), (s, t, got, oracle_dist)
    assert_path_valid(s, t, oracle_path, unit_profile, oracle_dist)
    er = edit(s, t, unit_profile)
    assert er.distance == pytest.approx(oracle_dist, abs=1e-9)
    assert replay(s, er.moves) == t
    assert rescore_moves(s, er.moves, unit_profile) == pytest.approx(er.distance, abs=1e-9)


def test_repeated_chars_long_known(unit_profile):
    # 长串不适合穷举 oracle；断言具体已知距离 + 路径重放/重算
    er = edit("mississippi", "missisippi", unit_profile)
    assert er.distance == pytest.approx(1.0)
    assert replay("mississippi", er.moves) == "missisippi"
    assert rescore_moves("mississippi", er.moves, unit_profile) == pytest.approx(1.0)


# ---------- 非对称代价 ----------

def test_asymmetric_insert_vs_delete_changes_optimum(weighted_profile):
    # delete=0.8 便宜于 insert=1.3：ab->a 应删；长度修复方向影响选择
    assert distance("abc", "a", weighted_profile) == pytest.approx(2 * 0.8)
    assert distance("a", "abc", weighted_profile) == pytest.approx(2 * 1.3)


def test_cheap_transpose_preferred_over_two_subs(weighted_profile):
    # transpose=0.6 < 2*sub=2.2：相邻交换应被选中
    got = edit("xy", "yx", weighted_profile)
    assert got.distance == pytest.approx(0.6)
    assert [m.type for m in got.moves] == ["swap"]


def test_directed_substitution_table_asymmetric():
    prof = cost_profile_from_dict({
        "insert": 1.0, "delete": 1.0, "substitute": 1.0, "transpose": 1.0,
        "substitute_table": {"0": {"o": 0.4}, "o": {"0": 0.9}},
    })
    assert distance("0", "o", prof) == pytest.approx(0.4)
    assert distance("o", "0", prof) == pytest.approx(0.9)


def test_asymmetric_costs_against_oracle(weighted_profile):
    pairs = [
        ("cat", "act"), ("0range", "orange"), ("abx", "ba"), ("x", "abc"),
        ("kitten", "sitteng"), ("ac", "ca"), ("oo00", "00oo"),
    ]
    for s, t in pairs:
        got = distance(s, t, weighted_profile)
        od, op = shortest_path(s, t, weighted_profile)
        assert got == pytest.approx(od, abs=1e-9), (s, t, got, od)
        er = edit(s, t, weighted_profile)
        assert rescore_moves(s, er.moves, weighted_profile) == pytest.approx(od, abs=1e-9)
        assert replay(s, er.moves) == t


# ---------- 随机对拍（固定种子，参考不由被测实现生成） ----------

_ALPHABETS = ["ab", "abc", "abcd", "abx"]


@pytest.mark.parametrize("seed", list(range(30)))
def test_random_fuzz_unit_costs(unit_profile, seed):
    rng = random.Random(1000 + seed)
    alpha = rng.choice(_ALPHABETS)
    s = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 4)))
    t = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 4)))
    got = distance(s, t, unit_profile)
    od, op = shortest_path(s, t, unit_profile)
    assert got == pytest.approx(od, abs=1e-9), (s, t, got, od)
    er = edit(s, t, unit_profile)
    assert replay(s, er.moves) == t
    assert rescore_moves(s, er.moves, unit_profile) == pytest.approx(od, abs=1e-9)
    assert_path_valid(s, t, op, unit_profile, od)


@pytest.mark.parametrize("seed", list(range(30)))
def test_random_fuzz_weighted(seed):
    rng = random.Random(2000 + seed)
    prof = cost_profile_from_dict({
        "insert": round(rng.uniform(0.5, 2.0), 2),
        "delete": round(rng.uniform(0.5, 2.0), 2),
        "substitute": round(rng.uniform(0.5, 2.0), 2),
        "transpose": round(rng.uniform(0.4, 2.0), 2),
        "substitute_table": {},
    })
    alpha = rng.choice(_ALPHABETS)
    s = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 4)))
    t = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 4)))
    got = distance(s, t, prof)
    od, op = shortest_path(s, t, prof)
    assert got == pytest.approx(od, abs=1e-9), (s, t, got, od, prof)
    er = edit(s, t, prof)
    assert replay(s, er.moves) == t
    assert rescore_moves(s, er.moves, prof) == pytest.approx(od, abs=1e-9)


# ---------- 零代价退化 ----------

def test_zero_costs_make_distance_zero():
    prof = cost_profile_from_dict({
        "insert": 0.0, "delete": 0.0, "substitute": 0.0, "transpose": 0.0,
        "substitute_table": {},
    })
    assert distance("abc", "xyzqq", prof) == 0.0


# ---------- 负代价必须被拒绝 ----------

def test_negative_cost_rejected():
    with pytest.raises(ValueError):
        cost_profile_from_dict({
            "insert": -1.0, "delete": 1.0, "substitute": 1.0, "transpose": 1.0,
            "substitute_table": {},
        })
