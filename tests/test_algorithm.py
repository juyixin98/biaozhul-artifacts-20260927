"""Core algorithm tests.

Every behavioural assertion is concrete: exact token sequences, exact
lengths, explicit costs. Short sentences are additionally cross-checked
against the independent brute-force oracle in ``tests/oracle.py`` (which
shares no implementation with the DAG DP).
"""
from __future__ import annotations

import math

import pytest

from app.algorithm import (
    GAP_CLEAR,
    GAP_CLOSE,
    GAP_NONE,
    TokenType,
    segment,
)
from app.lexicon import LexiconVersion, WordEntry, word_cost

from .oracle import SEED_WORDS, build_lex, dedupe_paths, enumerate_all_paths

UC = 20.0  # unknown char cost used in tests


# --------------------------------------------------------------------------
# Exhaustive verification against the independent oracle
# --------------------------------------------------------------------------

CLAUSES = [
    "研究生命",      # ambiguous: 研究|生命 vs 研究生|命
    "北京大学",      # nested longest-match ambiguity
    "哈哈哈哈",      # repeated words
    "星巴克",        # fully unknown -> no character dropped
    "研究生",       # word vs 研究 + 生
    "目的的确",      # overlapping ambiguity
    "研究",         # single word, no alternative
]


@pytest.mark.parametrize("clause", CLAUSES)
def test_best_and_second_best_match_exhaustive_oracle(clause):
    lex = build_lex(SEED_WORDS)
    result = segment(clause, lex, unknown_char_cost=UC)

    all_paths = dedupe_paths(enumerate_all_paths(clause, lex, UC))
    oracle_best = all_paths[0]
    # Distinct edge-sequence paths, sorted by the documented stable key.
    best_tokens = [(s.norm_start, s.norm_end, s.type, s.surface)
                   for s in result.segments]
    # Segments merge adjacent unknowns; expand them back to per-char edges
    # for comparison with the oracle representation.
    expanded = _expand_unknown_segments(best_tokens)
    assert expanded == list(oracle_best.tokens), (
        "best path disagrees with exhaustive enumeration"
    )
    assert result.best_cost == pytest.approx(oracle_best.cost, abs=1e-9)

    if len(all_paths) > 1:
        oracle_second = all_paths[1]
        assert result.second_best_cost == pytest.approx(oracle_second.cost, abs=1e-9)
        assert result.cost_gap == pytest.approx(
            oracle_second.cost - oracle_best.cost, abs=1e-9
        )
    else:
        assert result.second_best_cost is None
        assert result.cost_gap is None
        assert result.gap_class == GAP_NONE


def test_number_of_distinct_segmentations_yanjiushengming():
    # Pin the exact counts of distinct partitions of the ambiguous clause.
    lex = build_lex(SEED_WORDS)
    typed_paths = dedupe_paths(enumerate_all_paths("研究生命", lex, UC))
    # There are 12 distinct *typed* paths (dictionary edges vs one-character
    # unknown fallback edges). They collapse to 5 distinct surface strings
    # because a one-character dictionary word and an unknown edge render the
    # same surface. Both counts are enumerated independently of the DAG DP.
    assert len(typed_paths) == 12
    surfaces = sorted({tuple(t[3] for t in p.tokens) for p in typed_paths})
    assert surfaces == [
        ("研", "究", "生", "命"),
        ("研", "究", "生命"),
        ("研究", "生", "命"),
        ("研究", "生命"),
        ("研究生", "命"),
    ]
    # three fully-dictionary partitions exist and the optimum is cheapest
    full_dict = [p for p in typed_paths if all(t[2] == "dict" for t in p.tokens)]
    assert len(full_dict) == 3
    assert all(typed_paths[0].cost <= p.cost for p in typed_paths)


def _expand_unknown_segments(tokens):
    out = []
    for start, end, kind, surface in tokens:
        if kind == "unknown" and end - start > 1:
            for k in range(start, end):
                out.append((k, k + 1, "unknown", surface[k - start]))
        else:
            out.append((start, end, kind, surface))
    return out


# --------------------------------------------------------------------------
# Concrete optimal values (hand-derived expectations, hard-coded)
# --------------------------------------------------------------------------

def test_yanjiushengming_exact_optimum():
    lex = build_lex(SEED_WORDS)
    r = segment("研究生命", lex, unknown_char_cost=UC)
    assert [s.surface for s in r.segments] == ["研究", "生命"]
    assert [s.type for s in r.segments] == ["dict", "dict"]
    expected = word_cost(5000, sum(SEED_WORDS.values())) + word_cost(
        2000, sum(SEED_WORDS.values())
    )
    assert r.best_cost == pytest.approx(expected, abs=1e-9)
    # The famous alternative 研究生|命 must be the runner-up.
    assert r.gap_class == GAP_CLEAR
    assert r.decision == "accepted"


def test_second_best_is_yanjiusheng_pi():
    lex = build_lex(SEED_WORDS)
    r = segment("研究生命", lex, unknown_char_cost=UC)
    all_paths = dedupe_paths(enumerate_all_paths("研究生命", lex, UC))
    assert [t[3] for t in all_paths[1].tokens] == ["研究生", "命"]
    # and the reported gap corresponds to exactly that runner-up
    assert r.cost_gap == pytest.approx(
        all_paths[1].cost - all_paths[0].cost, abs=1e-9
    )


# --------------------------------------------------------------------------
# Unknown-word fallback: explicit length and cost, never drops a character
# --------------------------------------------------------------------------

def test_unknown_fallback_keeps_every_character():
    lex = build_lex({"咖啡": 100})
    r = segment("星巴克咖啡", lex, unknown_char_cost=UC)
    # lexicon knows 咖啡 but none of 星/星/巴/克
    types = [s.type for s in r.segments]
    assert types == ["unknown", "dict"]
    unknown_seg = r.segments[0]
    assert unknown_seg.surface == "星巴克"
    assert unknown_seg.norm_end - unknown_seg.norm_start == 3  # explicit length
    assert unknown_seg.cost == pytest.approx(3 * UC)  # explicit cost
    assert r.segments[1].surface == "咖啡"


def test_fully_unknown_text_cost_per_char():
    lex = build_lex({"x": 1})
    r = segment("αβγδ", lex, unknown_char_cost=UC)
    assert len(r.segments) == 1
    assert r.segments[0].type == "unknown"
    assert r.segments[0].surface == "αβγδ"
    assert r.segments[0].cost == pytest.approx(4 * UC)
    assert r.gap_class == GAP_NONE  # single-char fallback edges => one typed path


def test_coverage_is_contiguous_and_complete():
    lex = build_lex(SEED_WORDS)
    for clause in ["研究生命", "星巴克咖啡", "哈哈哈哈"]:
        r = segment(clause, lex, unknown_char_cost=UC)
        assert r.segments[0].raw_start == 0
        assert r.segments[-1].raw_end == len(clause)
        for a, b in zip(r.segments, r.segments[1:]):
            assert b.raw_start == a.raw_end, f"gap/overlap between {a!r} {b!r}"
        # normalized token concatenation reconstructs normalized text
        assert "".join(s.surface for s in r.segments) == r.normalized_text


# --------------------------------------------------------------------------
# Stable tie-break on equal cost
# --------------------------------------------------------------------------

def test_equal_cost_paths_break_by_stable_content_rule():
    # All four words equal freq => two partitions of "abcd" have exactly
    # equal cost; the winner is determined solely by content, never order.
    words = ["ab", "cd", "a", "bcd"]
    lex = LexiconVersion.build(1, [WordEntry(w, 1000) for w in words])
    r = segment("abcd", lex)
    # ("a","bcd") lexicographically precedes ("ab","cd")
    assert [s.surface for s in r.segments] == ["a", "bcd"]
    assert r.tie_broken is True
    assert r.cost_gap == 0.0
    # both partitions really are equal-cost
    p1 = word_cost(1000, 4000) + word_cost(1000, 4000)
    assert r.best_cost == pytest.approx(p1)
    assert r.second_best_cost == pytest.approx(p1)


def test_tie_break_is_independent_of_insertion_order():
    r1 = LexiconVersion.build(
        1, [WordEntry(w, 1000) for w in ["ab", "cd", "a", "bcd"]]
    )
    r2 = LexiconVersion.build(
        2, [WordEntry(w, 1000) for w in ["bcd", "a", "cd", "ab"]]
    )
    out1 = [s.surface for s in segment("abcd", r1).segments]
    out2 = [s.surface for s in segment("abcd", r2).segments]
    assert out1 == out2 == ["a", "bcd"]


def test_close_gap_is_indeterminate():
    freqs = {"ab": 1000, "cd": 1000, "a": 700, "bcd": 1000}
    lex = build_lex(freqs)
    r = segment("abcd", lex, close_gap_threshold=1.0)
    assert [s.surface for s in r.segments] == ["ab", "cd"]
    assert 0 < r.cost_gap < 1.0
    assert r.gap_class == GAP_CLOSE
    assert r.decision == "indeterminate"
    assert r.tie_broken is False


def test_clear_gap_is_accepted():
    lex = build_lex(SEED_WORDS)
    r = segment("研究生命", lex, close_gap_threshold=1.0)
    assert r.cost_gap > 1.0
    assert r.gap_class == GAP_CLEAR
    assert r.decision == "accepted"


# --------------------------------------------------------------------------
# Repeated words
# --------------------------------------------------------------------------

def test_repeated_word_segmentation():
    lex = build_lex({"哈哈": 300, "哈": 100})
    r = segment("哈哈哈哈", lex)
    assert [s.surface for s in r.segments] == ["哈哈", "哈哈"]
    assert all(s.type == "dict" for s in r.segments)
    # alternatives 哈哈|哈|哈 etc. exist and are strictly worse
    assert r.cost_gap > 0


# --------------------------------------------------------------------------
# Normalization offsets on the segmentation output
# --------------------------------------------------------------------------

def test_variable_length_normalization_offsets():
    # full-width input folds to a known ascii dictionary word
    lex = build_lex({"strasse": 100})
    r = segment("ｓｔｒａｓｓｅ", lex, unknown_char_cost=UC)
    assert r.normalized_text == "strasse"
    assert len(r.segments) == 1 and r.segments[0].type == "dict"
    seg = r.segments[0]
    # raw offsets cover all 7 raw characters; normalized span covers 7
    assert (seg.norm_start, seg.norm_end) == (0, 7)
    assert (seg.raw_start, seg.raw_end) == (0, 7)
    assert seg.raw_text == "ｓｔｒａｓｓｅ"


def test_eszett_expansion_maps_back_to_single_raw_char():
    # Build an entry so that normalized text 'strasse' is recognized even
    # though the raw input contains ß (1 raw char -> 2 norm chars).
    lex = build_lex({"strasse": 100})
    raw = "ｓｔｒａßｅ"  # 6 raw chars: 4 fullwidth, ß, 1 fullwidth
    r = segment(raw, lex, unknown_char_cost=UC)
    assert r.normalized_text == "strasse"
    seg = r.segments[0]
    assert seg.surface == "strasse"
    # raw text has 6 chars; coverage includes the ß exactly once
    assert len(raw) == 6
    assert seg.raw_start == 0 and seg.raw_end == 6
    assert seg.raw_text == raw
    assert seg.raw_text.count("ß") == 1
    # the expanded pair 'ss' (norm 4,5) maps to the single raw index 4
    assert r.char_map[4] == r.char_map[5] == 4
    assert r.char_map == (0, 1, 2, 3, 4, 4, 5)


def test_deleted_soft_hyphen_absorbed_into_adjacent_segment():
    lex = build_lex({"研究": 5000, "生命": 2000})
    raw = "研­究生命"
    r = segment(raw, lex)
    assert r.deleted_raw_indices == (1,)
    assert r.normalized_text == "研究生命"
    # the deleted U+00AD at raw index 1 is absorbed into the preceding segment
    assert r.segments[0].raw_text == "研­究"
    assert (r.segments[0].raw_start, r.segments[0].raw_end) == (0, 3)
    assert (r.segments[1].raw_start, r.segments[1].raw_end) == (3, 5)
    # full raw coverage, no gaps
    assert "".join(s.raw_text for s in r.segments) == raw


def test_leading_deleted_char_attached_to_first_segment():
    lex = build_lex({"ab": 100})
    r = segment("​ab", lex)
    assert r.segments[0].raw_start == 0
    assert r.segments[0].raw_end == 3
    assert r.segments[0].raw_text == "​ab"


def test_trailing_deleted_char_attached_to_last_segment():
    lex = build_lex({"ab": 100})
    r = segment("ab​", lex)
    assert r.segments[-1].raw_end == 3
    assert r.segments[-1].raw_text == "ab​"
