"""跨块一致性：同一份文本在不同分块方案下，命中多重集必须与朴素搜索相同。

这是本工程最核心的可复核性质：
    对任意分块，流匹配结果 ≡ 对完整文本的逐模式朴素搜索结果。
"""

from __future__ import annotations

import random

import pytest

from acstream.automaton import Automaton, Pattern
from conftest import CHUNK_PLANS, naive_find_all, split_into_chunks

# (描述, 模式表, 文本) 夹具：覆盖嵌套、后缀、自重叠、跨块、二进制。
CASES: list[tuple[str, list[tuple[str, bytes]], bytes]] = [
    (
        "suffix-chain",
        [("he", b"he"), ("she", b"she"), ("hers", b"hers")],
        b"she sells seashells ushers hers she",
    ),
    (
        "prefix-suffix-nesting",
        [("a", b"a"), ("aa", b"aa"), ("aaa", b"aaa"), ("aaaa", b"aaaa")],
        b"a" * 17 + b"baa",
    ),
    (
        "self-overlapping-periodic",
        [("abab", b"abab"), ("bab", b"bab"), ("b", b"b")],
        b"abababababXbabab",
    ),
    (
        "pattern_longer_than_each_chunk",
        [("needle", b"needle-in-a-haystack")],
        b"xxneedle-in-a-haystackyy",
    ),
    (
        "cross-boundary-starts",
        [("abc", b"abc"), ("bcd", b"bcd"), ("cde", b"cde")],
        b"zzabcdez" * 5,
    ),
    (
        "binary-alphabet",
        [
            ("z", b"\x00"),
            ("hdr", b"\xde\xad\xbe\xef"),
            ("trail", b"\xff\x00\xff"),
        ],
        b"\xde\xad" + b"\x00" * 3 + b"\xbe\xef" + b"\xff\x00\xff" + b"\xde\xad\xbe\xef",
    ),
    (
        "utf8-multibyte-boundaries",
        [("cn", "中文".encode()), ("mix", "a中".encode())],
        ("中文 a中 中文中".encode()),
    ),
    (
        "random-dense",
        [("ab", b"ab"), ("ba", b"ba"), ("aba", b"aba")],
        bytes(random.Random(245).choices([ord("a"), ord("b")], k=400)),
    ),
]


def stream_multiset(
    patterns: list[tuple[str, bytes]], data: bytes, plan: list[int]
) -> list[tuple[str, int, int]]:
    automaton = Automaton([Pattern(pid, d) for pid, d in patterns])
    state = 0
    offset = 0
    collected: list[tuple[str, int, int]] = []
    for chunk in split_into_chunks(data, plan):
        result = automaton.feed(state, chunk, offset)
        assert result.base_offset == offset
        state = result.state
        offset += result.length
        collected.extend((h.pattern_id, h.start, h.end) for h in result.hits)
    assert offset == len(data)
    collected.sort(key=lambda t: (t[2], -t[1], t[0]))
    return collected


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
@pytest.mark.parametrize("plan_name", sorted(CHUNK_PLANS))
def test_chunking_independence(case, plan_name) -> None:
    _name, patterns, data = case
    expected = naive_find_all(patterns, data)
    got = stream_multiset(patterns, data, CHUNK_PLANS[plan_name])
    assert got == expected, (
        f"分块方案 {plan_name} 下结果与朴素搜索不一致\n"
        f"missing={sorted(set(expected) - set(got))[:5]}\n"
        f"extra={sorted(set(got) - set(expected))[:5]}"
    )


@pytest.mark.parametrize("seed", [1, 7, 42, 245])
def test_random_chunk_sizes(seed: int) -> None:
    rng = random.Random(seed)
    patterns = [("p1", b"abc"), ("p2", b"bc"), ("p3", b"z")]
    data = bytes(rng.choices([97, 98, 99, 122], k=300))
    plan = [rng.randint(1, 7) for _ in range(300)]  # 最后会补零长尾块
    expected = naive_find_all(patterns, data)
    assert stream_multiset(patterns, data, plan) == expected


def test_hit_offsets_are_absolute_across_chunks() -> None:
    automaton = Automaton([Pattern("sig", b"abcd")])
    r1 = automaton.feed(0, b"xxab", 0)
    assert r1.hits == ()
    r2 = automaton.feed(r1.state, b"cdxx", 4)
    # 命中跨越块边界，位置必须按整个流的原始字节偏移报告。
    assert [(h.pattern_id, h.start, h.end) for h in r2.hits] == [("sig", 2, 6)]


def test_empty_chunks_do_not_move_offset_or_emit() -> None:
    automaton = Automaton([Pattern("a", b"a")])
    r1 = automaton.feed(0, b"", 0)
    assert r1.hits == () and r1.state == 0 and r1.length == 0
    r2 = automaton.feed(0, b"a", 0)
    assert [(h.start, h.end) for h in r2.hits] == [(0, 1)]
