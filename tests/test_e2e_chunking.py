"""End-to-end: same logical stream over HTTP, different chunkings.

Verifies the requirement directly at the service boundary: create a version,
stream identical bytes split different ways through the API, drain hits via
resumable pagination, and compare the hit multiset + exact offsets against the
independent naive oracle.
"""
from __future__ import annotations

import random

from tests.helpers import (
    create_version, feed, hit_tuples, open_scan, page_all,
)
from tests.oracle import naive_stream

PATTERN_SETS = [
    # prefix/suffix nesting
    [b"abc", b"bc", b"c"],
    # classic
    [b"he", b"she", b"his", b"hers"],
    # heavy self overlap + binary
    [b"\x00\x00", b"\x00\x00\x01", b"\xff"],
    [b"abab", b"bab", b"ab"],
]


def _chunkify(data: bytes, sizes):
    out, i = [], 0
    for s in sizes:
        if i >= len(data):
            break
        out.append(data[i:i + s])
        i += s
    if i < len(data):
        out.append(data[i:])
    return out


def test_http_chunkings_produce_identical_multiset(client):
    rng = random.Random(7)
    data = bytes(rng.choice(b"abcdefghsihers\x00\xff") for _ in range(150))

    for pi, patterns in enumerate(PATTERN_SETS):
        want = sorted(naive_stream([data], patterns))
        chunk_plans = [
            [len(data)],
            [1] * len(data),
            [5] * (len(data) // 5 + 1),
            [rng.randint(1, 13) for _ in range(len(data))],
        ]
        for ci, sizes in enumerate(chunk_plans):
            chunks = _chunkify(data, sizes)
            v = create_version(client, patterns, encoding="binary")
            sid = open_scan(client, v)
            for ch in chunks:
                feed(client, sid, ch)
            got = hit_tuples(page_all(client, sid, limit=7))
            assert got == want, (
                f"pattern set {pi}, chunk plan {ci} "
                f"({[len(c) for c in chunks][:10]}...): multiset differs"
            )


def test_http_byte_offsets_are_absolute_and_concrete(client):
    v = create_version(client, [b"abc", b"bc", b"c"], encoding="binary")
    sid = open_scan(client, v)
    feed(client, sid, b"xxa")      # split the pattern across two chunks
    feed(client, sid, b"bcx")
    hits = page_all(client, sid, limit=2)
    concrete = {(h["start"], h["end"], h["pattern_id"]) for h in hits}
    assert concrete == {
        (2, 5, 0),  # abc
        (3, 5, 1),  # bc
        (4, 5, 2),  # c
    }
    for h in hits:
        assert h["end"] - h["start"] == h["pattern_length"]
