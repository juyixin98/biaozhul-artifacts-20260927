"""算法索引层测试。

- 对组合符/旗帜/ZWJ/肤色/键帽/CRLF 写死具体索引数组；
- 与独立预言 regex \\X 在大量合成输入上逐簇对照；
- 合法边界往返映射；非法字节位置/非簇边界必须给出 NOT_A_BOUNDARY；
- 增量编辑与完整重建逐数组一致（含具体断言，不只是“不抛异常”）。
"""
from __future__ import annotations

import random

import pytest

from app.errors import (
    NotABoundaryError,
    PositionOutOfRangeError,
    UnsupportedSpaceError,
)
from app.indexing import (
    apply_edit,
    build_index,
    plan_edit,
)

from conftest import (
    oracle_byte_length,
    oracle_byte_starts,
    oracle_cluster_starts,
    oracle_clusters,
)

CRLF = "\r\n"
ZWJ = "‍"


# ── 写死的具体索引值 ─────────────────────────────────────────────────────────
EXPECTED = [
    # (文本, cp_to_byte, cluster_to_cp)
    ("\u00e9", (0, 2), (0, 1)),
    ("e\u0301", (0, 1, 3), (0, 2)),
    ("é", (0, 1, 3), (0, 2)),
    ("🇺🇳", (0, 4, 8), (0, 2)),
    ("👨‍👩", (0, 4, 7, 11), (0, 3)),
    ("👦🏽", (0, 4, 8), (0, 2)),
    ("#️⃣", (0, 1, 4, 7), (0, 3)),
    ("a\r\nb", (0, 1, 2, 3, 4), (0, 1, 3, 4)),
]


@pytest.mark.parametrize("text,cp2b,cl2cp", EXPECTED)
def test_concrete_index_arrays(text, cp2b, cl2cp):
    idx = build_index(text)
    assert idx.cp_to_byte == cp2b
    assert idx.cluster_to_cp == cl2cp
    # cp_to_cluster 由簇起点展开，逐点核对
    cp_to_cluster = []
    for c, (a, b) in enumerate(zip(cl2cp, cl2cp[1:])):
        cp_to_cluster += [c] * (b - a)
    assert idx.cp_to_cluster == tuple(cp_to_cluster)
    assert idx.cluster_to_byte == tuple(cp2b[c] for c in cl2cp)
    assert idx.byte_count == cp2b[-1]
    assert idx.cp_count == len(text)
    assert idx.cluster_count == len(cl2cp) - 1


MIXED = "aé🇺🇳👨‍👩‍👧\r\nb"


def test_mixed_detailed():
    idx = build_index(MIXED)
    assert idx.cp_to_byte == (0, 1, 3, 7, 11, 15, 18, 22, 25, 29, 30, 31, 32)
    assert idx.cluster_to_cp == (0, 1, 2, 4, 9, 11, 12)
    assert idx.cluster_count == 6
    # 簇文本必须整体读出（不能按字符数切）
    assert [idx.cluster_text(c) for c in range(6)] == [
        "a", "é", "🇺🇳", "👨‍👩‍👧", CRLF, "b",
    ]
    assert idx.byte_count == 32
    assert idx.cp_count == 12


# ── 独立预言：簇划分与字节数组 ───────────────────────────────────────────────
POOL = list(
    "ab eé\t"
    + "́̈⃣"
    + "‍"
    + "👨👩👧👦🏽❤️#*"
    + "🇦🇧🇨🇽"
    + "\r\n\0"
    + "ᄀᄂᆨᆫ가"
)


def test_oracle_alignment_many_inputs():
    rng = random.Random(77)
    for _ in range(2000):
        text = "".join(rng.choice(POOL) for _ in range(rng.randrange(0, 40)))
        idx = build_index(text)
        # 簇起点 == regex \X 预言
        assert idx.cluster_to_cp == tuple(oracle_cluster_starts(text))
        # 字节起点 == 手工编码预言
        assert idx.cp_to_byte == tuple(oracle_byte_starts(text))
        assert idx.byte_count == oracle_byte_length(text)
        # 每个簇文本拼接后还原原文
        pieces = [idx.cluster_text(c) for c in range(idx.cluster_count)]
        assert "".join(pieces) == text
        assert oracle_clusters(text) == pieces


# ── 位置转换往返 ─────────────────────────────────────────────────────────────
def test_position_conversion_roundtrip():
    idx = build_index(MIXED)
    # 每个簇起点三种坐标一致
    for c in range(idx.cluster_count + 1):
        cp = idx.to_codepoints(c, "cluster")
        b = idx.to_bytes(c, "cluster")
        assert idx.to_clusters(cp, "codepoint") == c
        assert idx.to_clusters(b, "byte") == c
        assert idx.to_bytes(cp, "codepoint") == b
        assert idx.to_codepoints(b, "byte") == cp
        # convert 与直连方法一致
        assert idx.convert(c, "cluster", "byte") == b
        assert idx.convert(b, "byte", "cluster") == c


def test_byte_position_inside_multibyte_rejected():
    idx = build_index("éx")
    # é 占 2 字节，偏移 1 在多字节序列内部
    with pytest.raises(NotABoundaryError) as ei:
        idx.to_codepoints(1, "byte")
    assert ei.value.code == "NOT_A_BOUNDARY"
    d = ei.value.details
    assert d["input_position"] == 1
    assert d["nearest_boundaries"] == {"start_byte": 0, "end_byte": 2}


def test_edit_inside_cluster_rejected():
    # e + combining acute 是一个簇，码点 1 在簇内部
    idx = build_index("éz")
    with pytest.raises(NotABoundaryError) as ei:
        idx.validate_edit_position(1, "codepoint")
    d = ei.value.details
    assert d["nearest_boundaries"]["cluster"] == 0
    assert d["nearest_boundaries"]["cluster_start_codepoint"] == 0
    assert d["nearest_boundaries"]["cluster_end_codepoint"] == 2


def test_flag_interior_position_rejected():
    idx = build_index("🇺🇳")
    # 旗帜由 2 个 RI 码点构成：码点 1 不是编辑边界（不能只删半个旗）
    with pytest.raises(NotABoundaryError):
        idx.validate_edit_position(1, "codepoint")
    # 但字节 4（第二个 RI 起点）同样不是簇边界
    with pytest.raises(NotABoundaryError):
        idx.validate_edit_position(4, "byte")


def test_range_and_space_errors():
    idx = build_index("ab")
    with pytest.raises(PositionOutOfRangeError):
        idx.to_bytes(99, "byte")
    with pytest.raises(PositionOutOfRangeError):
        idx.to_clusters(-1, "codepoint")
    with pytest.raises(UnsupportedSpaceError):
        idx.convert(0, "word", "byte")


def test_plan_edit_ordering():
    idx = build_index("abc")
    with pytest.raises(PositionOutOfRangeError):
        plan_edit(idx, 2, 1, "codepoint", "")


# ── 增量 vs 完整重建：具体断言 ───────────────────────────────────────────────
def edit_and_compare(text, start_space_pos, end_space_pos, space, repl):
    idx = build_index(text)
    edit = plan_edit(idx, start_space_pos, end_space_pos, space, repl)
    inc = apply_edit(idx, edit)
    full = build_index(inc.text)
    assert inc.text == full.text
    assert inc.cp_to_byte == full.cp_to_byte
    assert inc.cp_to_cluster == full.cp_to_cluster
    assert inc.cluster_to_cp == full.cluster_to_cp
    assert inc.cluster_to_byte == full.cluster_to_byte
    return idx, inc


def test_edit_inserts_combining_merges_with_left():
    # 在 'a|b' 中插入 combining acute(U+0301)：a+U+0301 合并为一个簇
    idx, inc = edit_and_compare("ab", 1, 1, "codepoint", "\u0301")
    assert inc.text == "a\u0301b"
    assert inc.cluster_count == 2
    assert inc.cluster_text(0) == "a\u0301"
    assert inc.cluster_text(1) == "b"

def test_edit_delete_second_ri_of_flag():
    # 删除旗帜第二码点不允许；删除整个旗帜簇后结果必须等于全量重建
    idx = build_index("x🇺🇳y")
    assert idx.cluster_count == 3
    # 旗帜簇是第 1 簇
    edit = plan_edit(idx, 1, 2, "cluster", "")
    inc = apply_edit(idx, edit)
    full = build_index("xy")
    assert inc.cluster_to_cp == full.cluster_to_cp
    assert inc.cluster_to_byte == full.cluster_to_byte
    assert inc.text == "xy"


def test_edit_insert_ri_between_flags():
    # 3 个 RI：新插入 1 个 RI 改变奇偶配对，增量结果必须与全量重切一致
    idx = build_index("🇦🇧🇨")
    assert idx.cluster_count == 2  # AB 旗 + C 单
    edit = plan_edit(idx, 2, 2, "codepoint", "🇽")
    inc = apply_edit(idx, edit)
    full = build_index(inc.text)
    assert inc.cluster_to_cp == full.cluster_to_cp
    # 具体结果：A B | X C
    assert inc.cluster_to_cp == (0, 2, 4)


def test_edit_crlf_inside_pair():
    idx = build_index("a\r\nb")
    # 删除 CR（簇边界 1 是 CRLF 簇起点，删除整簇到 b 前）
    edit = plan_edit(idx, 1, 2, "cluster", "X")
    inc = apply_edit(idx, edit)
    full = build_index(inc.text)
    assert inc.cluster_to_cp == full.cluster_to_cp
    assert inc.text == "aXb"
    # 在 CR 与 LF 之间不能插入：CRLF 簇内部位置非法
    with pytest.raises(NotABoundaryError):
        plan_edit(idx, 2, 2, "codepoint", "X")


def test_edit_split_crlf_by_inserting_before_lf():
    # 文本 'a\r\nb'，在码点 2（LF 上，非簇边界）插入应被拒绝
    idx = build_index("a\r\nb")
    with pytest.raises(NotABoundaryError):
        plan_edit(idx, 2, 2, "codepoint", "x")


def test_edit_zwj_sequence_rejoin_across_seam():
    # '👨' + [插入 ZWJ👩] → ZWJ 序列合并
    idx = build_index("👨x")
    edit = plan_edit(idx, 1, 1, "codepoint", "‍👩")
    inc = apply_edit(idx, edit)
    full = build_index(inc.text)
    assert inc.cluster_to_cp == full.cluster_to_cp
    assert inc.cluster_count == 2
    assert inc.cluster_text(0) == "👨‍👩"


def test_byte_space_edit_concrete():
    idx = build_index("aéb")
    # é 起始字节是 1；在其后（字节 3）插入字母 x
    edit = plan_edit(idx, 3, 3, "byte", "x")
    inc = apply_edit(idx, edit)
    full = build_index("aéxb")
    assert inc.cp_to_byte == full.cp_to_byte
    assert inc.cp_to_byte == (0, 1, 3, 4, 5)


def test_incremental_matches_rebuild_randomized():
    rng = random.Random(2026)
    checked = 0
    for _ in range(5000):
        text = "".join(rng.choice(POOL) for _ in range(rng.randrange(0, 25)))
        idx = build_index(text)
        starts = list(idx.cluster_to_cp)
        cp1 = rng.choice(starts)
        cp2 = rng.choice([c for c in starts if c >= cp1])
        space = rng.choice(("cluster", "codepoint", "byte"))
        if space == "cluster":
            mp = {st: c for c, st in enumerate(idx.cluster_to_cp)}
            p1, p2 = mp[cp1], mp[cp2]
        elif space == "byte":
            p1, p2 = idx.cp_to_byte[cp1], idx.cp_to_byte[cp2]
        else:
            p1, p2 = cp1, cp2
        repl = "".join(rng.choice(POOL) for _ in range(rng.randrange(0, 10)))
        edit = plan_edit(idx, p1, p2, space, repl)
        inc = apply_edit(idx, edit)
        full = build_index(inc.text)
        assert inc.cp_to_byte == full.cp_to_byte
        assert inc.cp_to_cluster == full.cp_to_cluster
        assert inc.cluster_to_cp == full.cluster_to_cp
        checked += 1
    assert checked == 5000
