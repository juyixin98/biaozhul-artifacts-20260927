"""Cross-chunk streaming tests.

The key invariant: for the same pattern set and the same logical byte stream,
the hit MULTISET must be identical for every chunking. Offsets are raw byte
offsets into the whole stream, not per-chunk. Ground truth is the independent
naive oracle.
"""
from __future__ import annotations

import itertools
import os

import pytest

from app.automaton import Automaton
from app.spec import TextSpec, CaseMode
from app.matcher import StreamingMatcher
from tests.oracle import naive_stream


def _stream(patterns, chunks, *, encoding="binary",
            case_mode=CaseMode.SENSITIVE, fold_patterns=False):
    from app.spec import ascii_casefold
    pats = ([ascii_casefold(p) for p in patterns]
            if fold_patterns else patterns)
    a = Automaton(pats)
    m = StreamingMatcher("v1", a, TextSpec(encoding=encoding,
                                           case_mode=case_mode))
    all_hits = []
    for ch in chunks:
        all_hits.extend(m.push(ch))
    return all_hits, m


def test_cross_chunk_hit_in_two_pieces():
    patterns = [b"abcde"]
    hits, m = _stream(patterns, [b"xxab", b"cdexx"])
    assert sorted(hits) == [(2, 7, 0)]
    assert m.status().bytes_consumed == 9
    assert m.status().state_node == 0  # 'xx' tail returns to root


def test_pattern_split_at_every_boundary():
    patterns = [b"abcdef"]
    text = b"00abcdef00"
    # Cut after each possible position 1..len-1.
    for cut in range(1, len(text)):
        chunks = [text[:cut], text[cut:]]
        hits, _ = _stream(patterns, chunks)
        assert sorted(hits) == [(2, 8, 0)], f"failed at cut={cut}"


def test_multibyte_utf8_offsets_are_byte_offsets():
    # "é" is 2 bytes in UTF-8 (0xC3 0xA9). Split the multibyte character
    # itself across two chunks; the pattern "abc" must still start at byte 2.
    patterns = [b"abc"]
    data = "éabc".encode("utf-8")
    assert data == b"\xc3\xa9abc"
    hits, m = _stream(patterns, [data[:1], data[1:]], encoding="utf-8")
    assert sorted(hits) == [(2, 5, 0)]
    assert m.status().bytes_consumed == 5


def test_empty_chunk_does_not_shift_offsets():
    patterns = [b"ab"]
    hits, m = _stream(patterns, [b"", b"a", b"", b"b", b""])
    assert sorted(hits) == [(0, 2, 0)]
    assert m.status().bytes_consumed == 2


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_random_chunkings_match_oracle_multiset(seed):
    rng = __import__("random").Random(seed)
    patterns = [b"ab", b"bc", b"abc", b"cba", b"a"]
    text = bytes(rng.choice(b"abc") for _ in range(120))

    want = sorted(naive_stream([text], patterns))

    # Random chunk sizes incl. 1 (every byte separate) and huge (one chunk).
    for chunk_sizes in (
        [1] * len(text),
        [len(text)],
        [3] * (len(text) // 3 + 1),
        [rng.randint(1, 9) for _ in range(len(text))],
        [rng.randint(1, 30) for _ in range(len(text))],
    ):
        chunks, i = [], 0
        for size in chunk_sizes:
            if i >= len(text):
                break
            chunks.append(text[i:i + size])
            i += size
        if i < len(text):
            chunks.append(text[i:])
        hits, m = _stream(patterns, chunks)
        assert sorted(hits) == want, \
            f"multiset differs for chunking {[len(c) for c in chunks]}"
        assert m.status().bytes_consumed == len(text)


def test_chain_suffix_across_chunks_matches_oracle():
    patterns = [b"zabcd", b"abcd", b"bcd", b"cd", b"d"]
    text = b"xxzabcdyy"
    want = sorted(naive_stream([text], patterns))
    for cut in range(1, len(text)):
        chunks = [text[:cut], text[cut:]]
        hits, _ = _stream(patterns, chunks)
        assert sorted(hits) == want, f"cut={cut}"


def test_binary_data_random_vs_oracle():
    rng = __import__("random").Random(42)
    patterns = [b"\x00\x01", b"\x01\x02", b"\x00", b"\xff\xff\xff"]
    data = bytes(rng.randrange(256) for _ in range(300))
    chunks = [data[i:i + 7] for i in range(0, len(data), 7)]
    hits, _ = _stream(patterns, chunks)
    assert sorted(hits) == sorted(naive_stream([data], patterns))


def test_ascii_casefold_streaming_matches_folded_oracle():
    # Patterns and chunks carry ORIGINAL case; the spec layer folds them.
    raw_patterns = [b"ABC", b"bC"]
    from app.spec import ascii_casefold
    folded_patterns = [ascii_casefold(p) for p in raw_patterns]
    text = b"aBcAbC"
    folded_text = ascii_casefold(text)
    # Split mid-stream; the matcher folds chunks; patterns are folded the
    # same way the version service folds them at compile time.
    hits, _ = _stream(raw_patterns, [text[:2], text[2:]],
                      encoding="latin-1", case_mode=CaseMode.ASCII_CASEFOLD,
                      fold_patterns=True)
    assert sorted(hits) == sorted(naive_stream([folded_text], folded_patterns))
    # Concrete expected positions:
    # abc at 0..3 and 3..6 ; bc at 1..3 and 4..6
    assert sorted(hits) == sorted([
        (0, 3, 0), (1, 3, 1), (3, 6, 0), (4, 6, 1),
    ])


def test_invalid_utf8_chunk_rejected_without_state_change():
    a = Automaton([b"ab"])
    m = StreamingMatcher("v1", a, TextSpec(encoding="utf-8"))
    m.push(b"a")  # valid prefix
    from app.errors import EncodingError
    with pytest.raises(EncodingError):
        m.push(b"\xff")  # invalid start byte
    # State unchanged: a following valid "b" must NOT match "ab" across the
    # rejected chunk, and consumed bytes exclude the rejected payload.
    m.push(b"b")
    st = m.status()
    assert st.bytes_consumed == 2
