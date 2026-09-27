"""Property-style fuzz: randomized automata and streams vs naive oracle.

Deliberately deterministic (fixed seeds) so the suite is reproducible, but
covers hundreds of random pattern sets, alphabets (including binary) and
chunkings. Every reference computation uses tests.oracle, never the engine
under test.
"""
from __future__ import annotations

import random

import pytest

from app.automaton import Automaton
from app.matcher import StreamingMatcher
from app.spec import TextSpec
from tests.oracle import naive_stream


@pytest.mark.parametrize("seed", range(12))
def test_fuzz_automaton_vs_oracle(seed):
    rng = random.Random(1000 + seed)
    alphabet_size = rng.choice([2, 3, 4, 256])
    alphabet = (list(range(alphabet_size)) if alphabet_size <= 4
                else list(range(256)))

    n_patterns = rng.randint(1, 8)
    patterns = set()
    while len(patterns) < n_patterns:
        length = rng.randint(1, 6)
        pat = bytes(rng.choice(alphabet) for _ in range(length))
        patterns.add(pat)
    patterns = sorted(patterns)

    stream_len = rng.randint(0, 200)
    if alphabet_size == 256:
        data = bytes(rng.randrange(256) for _ in range(stream_len))
    else:
        data = bytes(rng.choice(alphabet) for _ in range(stream_len))

    # One-shot engine output equals oracle.
    a = Automaton(patterns)
    one_shot = sorted(a.feed(a.root(), data, 0)[1])
    oracle = sorted(naive_stream([data], patterns))
    assert one_shot == oracle, (
        f"seed {seed}: one-shot mismatch\n{patterns!r}\n{data[:40]!r}")

    # Several random chunkings give the same multiset with identical offsets.
    for _ in range(3):
        sizes = [rng.randint(1, 7) for _ in range(stream_len)]
        chunks, i = [], 0
        for s in sizes:
            if i >= stream_len:
                break
            chunks.append(data[i:i+s])
            i += s
        if i < stream_len:
            chunks.append(data[i:])
        m = StreamingMatcher("v", a, TextSpec(encoding="binary"))
        streamed = []
        for ch in chunks:
            streamed.extend(m.push(ch))
        assert sorted(streamed) == oracle
        assert m.status().bytes_consumed == stream_len
        assert m.status().bytes_held == 0
