"""自动机单元测试：失败指针/输出链、嵌套、二进制、拒绝类别。"""

from __future__ import annotations

import pytest

from acstream.automaton import Automaton, Pattern
from acstream.errors import ErrorCode
from acstream.errors import ApiError


def make(patterns: list[tuple[str, bytes]]) -> Automaton:
    return Automaton([Pattern(pid, d) for pid, d in patterns])


def test_suffix_via_failure_link_ushehers() -> None:
    # 教材用例：she 的终止节点必须经失败链带出后缀 he。
    a = make([("he", b"he"), ("she", b"she"), ("his", b"his"), ("hers", b"hers")])
    assert [(h.pattern_id, h.start, h.end) for h in a.search(b"ushers")] == [
        ("she", 1, 4),
        ("he", 2, 4),
        ("hers", 2, 6),
    ]


def test_suffix_pattern_is_never_missed() -> None:
    # 长词结束的同一位置必须报告其全部后缀模式。
    a = make([("a", b"a"), ("ba", b"ba"), ("cba", b"cba")])
    hits = {(h.pattern_id, h.start, h.end) for h in a.search(b"xcba")}
    assert ("cba", 1, 4) in hits
    assert ("ba", 2, 4) in hits
    assert ("a", 3, 4) in hits


def test_prefix_suffix_nesting_all_combos() -> None:
    a = make([("a", b"a"), ("aa", b"aa"), ("aaa", b"aaa")])
    got = {(h.pattern_id, h.start, h.end) for h in a.search(b"aaaa")}
    # 每个可能起点/长度的命中都应出现（共 4+3+2=9）；用集合比较多重集身份。
    expected = set(
        [("a", i, i + 1) for i in range(4)]
        + [("aa", i, i + 2) for i in range(3)]
        + [("aaa", i, i + 3) for i in range(2)]
    )
    assert got == expected


def test_overlapping_self_overlap() -> None:
    a = make([("aa", b"aa")])
    assert [(h.start, h.end) for h in a.search(b"aaaaa")] == [
        (0, 2), (1, 3), (2, 4), (3, 5)
    ]


def test_binary_patterns_full_byte_range() -> None:
    # 包含 0x00、0xFF 与跨字节模式，验证字节域而非字符域。
    a = make([("nul", b"\x00"), ("ff00", b"\xff\x00"), ("pair", b"\x01\x02\x00")])
    data = b"\x00\xff\x00\x01\x02\x00\xff"
    got = sorted((h.pattern_id, h.start, h.end) for h in a.search(data))
    assert got == [
        ("ff00", 1, 3),
        ("nul", 0, 1),
        ("nul", 2, 3),
        ("nul", 5, 6),
        ("pair", 3, 6),
    ]


def test_empty_pattern_rejected_with_specific_code() -> None:
    with pytest.raises(ApiError) as exc:
        make([("p1", b"a"), ("empty", b"")])
    assert exc.value.code == ErrorCode.EMPTY_PATTERN
    assert exc.value.http_status == 422
    assert exc.value.details["pattern_id"] == "empty"
    assert exc.value.outcome == "reject"


def test_empty_pattern_set_rejected() -> None:
    with pytest.raises(ApiError) as exc:
        make([])
    assert exc.value.code == ErrorCode.EMPTY_PATTERN_SET


def test_duplicate_pattern_id_rejected() -> None:
    with pytest.raises(ApiError) as exc:
        make([("dup", b"abc"), ("dup", b"def")])
    assert exc.value.code == ErrorCode.DUPLICATE_PATTERN_ID


def test_fingerprint_is_content_addressed_and_stable() -> None:
    a1 = make([("x", b"abc"), ("y", b"abd")])
    # 顺序不同的同一多重集必须得到相同指纹。
    a2 = make([("y", b"abd"), ("x", b"abc")])
    assert a1.fingerprint == a2.fingerprint
    a3 = make([("x", b"abc"), ("y", b"abe")])
    assert a3.fingerprint != a1.fingerprint


def test_feed_rejects_foreign_state_node() -> None:
    a1 = make([("he", b"he")])
    a2 = make([("world", b"world")])  # 不同 trie，节点号布局不同
    # a2 的某节点号在 a1 上可能越界或语义错误；越界必须被拒绝。
    foreign = a2.node_count - 1
    if foreign < a1.node_count:
        pytest.skip("节点号恰好未越界；版本混用主要由服务层指纹校验拦截")
    with pytest.raises(ValueError):
        a1.feed(foreign, b"he", 0)
