"""Unit tests for the compiled automaton: failure links + output chain.

These assert concrete, hand-computed outcomes — they do not check merely that
an API is callable.
"""
from __future__ import annotations

import pytest

from app.automaton import Automaton, AutomatonBuildError
from tests.oracle import naive_match


# ---- construction guards -----------------------------------------------------

def test_empty_pattern_rejected_by_automaton():
    with pytest.raises(AutomatonBuildError):
        Automaton([b""])


def test_empty_pattern_set_rejected():
    with pytest.raises(AutomatonBuildError):
        Automaton([])


def test_duplicate_patterns_rejected():
    with pytest.raises(AutomatonBuildError):
        Automaton([b"he", b"he"])


def test_pattern_ids_follow_insertion_order():
    a = Automaton([b"abcd", b"bc"])
    assert a.patterns == (b"abcd", b"bc")
    assert a.pattern_length(0) == 4
    assert a.pattern_length(1) == 2


# ---- classic failure-link / output-chain cases ------------------------------

def test_he_she_his_hers_classic_example():
    # The textbook AC example. Expected terminal positions are exact.
    patterns = [b"he", b"she", b"his", b"hers"]
    a = Automaton(patterns)
    text = b"ushers"
    hits = a.feed(a.root(), text, 0)[1]
    # ushers:
    #  she : s=1 e=4
    #  he  : s=2 e=4   (suffix of she, reached via output chain)
    #  hers: s=2 e=6
    assert sorted(hits) == sorted([
        (1, 4, 1),
        (2, 4, 0),
        (2, 6, 3),
    ])


def test_suffix_pattern_never_missed_via_output_chain():
    # Prefix/suffix nesting: long pattern contains shorter ones at its tail.
    patterns = [b"abc", b"bc", b"c"]
    a = Automaton(patterns)
    hits = a.feed(a.root(), b"xabc", 0)[1]
    assert sorted(hits) == sorted([
        (1, 4, 0),  # abc
        (2, 4, 1),  # bc  (strict suffix)
        (3, 4, 2),  # c   (strict suffix of the suffix)
    ])


def test_chained_output_links_depth_three():
    patterns = [b"zabcd", b"abcd", b"bcd", b"cd", b"d"]
    a = Automaton(patterns)
    hits = a.feed(a.root(), b"zabcd", 0)[1]
    # All five terminate at the final 'd'; output chain must enumerate all.
    expected = {
        (0, 5, 0),
        (1, 5, 1),
        (2, 5, 2),
        (3, 5, 3),
        (4, 5, 4),
    }
    assert set(hits) == expected


def test_no_phantom_hits():
    a = Automaton([b"abc"])
    assert a.feed(a.root(), b"abd", 0)[1] == []
    assert a.feed(a.root(), b"cab", 0)[1] == []


def test_overlapping_self_overlap_aaaa():
    a = Automaton([b"aa"])
    hits = a.feed(a.root(), b"aaaa", 0)[1]
    # "aa" at (0,2),(1,3),(2,4): overlaps must all appear.
    assert sorted(hits) == [(0, 2, 0), (1, 3, 0), (2, 4, 0)]


def test_overlapping_prefix_prefix():
    # "abab" contains "ab" twice and "bab" once, nested.
    a = Automaton([b"ab", b"bab"])
    hits = a.feed(a.root(), b"abab", 0)[1]
    assert sorted(hits) == [
        (0, 2, 0),
        (1, 4, 1),
        (2, 4, 0),
    ]


def test_binary_pattern_with_nul_and_high_bytes():
    patterns = [b"\x00\xff\x00", b"\xff"]
    a = Automaton(patterns)
    data = b"\x00\xff\x00\xff"
    hits = a.feed(a.root(), data, 0)[1]
    expected = sorted([
        (0, 3, 0),   # \x00\xff\x00
        (1, 2, 1),   # \xff
        (3, 4, 1),   # \xff
    ])
    assert sorted(hits) == expected


def test_transition_is_deterministic_and_root_restart():
    a = Automaton([b"ab"])
    s = a.root()
    s = a.transition(s, ord("x"))
    assert s == 0                       # unmatched byte returns to root
    s = a.transition(s, ord("a"))
    s = a.transition(s, ord("b"))
    assert list(a.outputs_at(s)) == [0]


@pytest.mark.parametrize("patterns,text", [
    ([b"he", b"she", b"his", b"hers"], b"ushershishe"),
    ([b"a", b"aa", b"aaa", b"aaaa"], b"aaaaaa"),
    ([b"\x00", b"\x00\x01", b"\xff\xff"], b"\x00\x01\x00\xff\xff"),
    ([b"ab", b"ba", b"aba", b"bab"], b"abababab"),
])
def test_matches_naive_oracle_one_shot(patterns, text):
    a = Automaton(patterns)
    got = sorted(a.feed(a.root(), text, 0)[1])
    want = sorted(naive_match(text, patterns))
    assert got == want
