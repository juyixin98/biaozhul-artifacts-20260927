"""纯随机对拍：随机模式表 + 随机文本 + 随机分块，对朴素搜索多重集。

参考实现（conftest.naive_find_all）与被测 AC 完全独立。
"""

from __future__ import annotations

import random
import string

import pytest

from acstream.automaton import Automaton, Pattern
from conftest import naive_find_all, split_into_chunks


def random_case(rng: random.Random):
    alphabet_size = rng.choice([2, 3, 4])
    alphabet = [ord(c) for c in string.ascii_lowercase[:alphabet_size]]
    n_patterns = rng.randint(1, 12)
    patterns: list[tuple[str, bytes]] = []
    for i in range(n_patterns):
        length = rng.randint(1, 8)
        patterns.append((f"p{i}", bytes(rng.choices(alphabet, k=length))))
    # 去重模式 id 冲突（随机内容可能同 id 不同内容——不会；但可能同内容）。
    dedup: dict = {}
    for pid, d in patterns:
        dedup.setdefault(d, pid)
    patterns = [(pid, d) for d, pid in dedup.items()]
    data = bytes(rng.choices(alphabet, k=rng.randint(0, 200)))
    plan = [rng.randint(0, 9) for _ in range(rng.randint(0, 40))]
    return patterns, data, plan


def stream_hits(patterns, data, plan):
    auto = Automaton([Pattern(pid, d) for pid, d in patterns])
    state, offset, out = 0, 0, []
    for chunk in split_into_chunks(data, plan):
        r = auto.feed(state, chunk, offset)
        state, offset = r.state, offset + r.length
        out.extend((h.pattern_id, h.start, h.end) for h in r.hits)
    # 与预言机相同的规范顺序，逐元素比较。
    return sorted(out, key=lambda t: (t[2], -t[1], t[0]))


@pytest.mark.parametrize("seed", range(60))
def test_random_equivalence_against_naive(seed: int) -> None:
    rng = random.Random(1000 + seed)
    patterns, data, plan = random_case(rng)
    expected = naive_find_all(patterns, data)
    assert stream_hits(patterns, data, plan) == expected
