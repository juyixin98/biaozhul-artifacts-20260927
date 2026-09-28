"""Tests for the RE2-backed scanner: zero-width advance and adjacent matches."""

from __future__ import annotations

import re as stdlib_re

import pytest

from app.engine import EngineOptions, compile_pattern, scan
from app.errors import PatternBudgetExceededError, UnsupportedSyntaxError
from app.textspec import ByteIndex


def spans(compiled, data):
    return [(c.start, c.end) for c in scan(compiled, data, ByteIndex(data))]


def test_zero_width_a_star_matches_stdlib_advancing_rule(run_logger, request):
    """a* on 'bab': stdlib emits (0,0),(1,2),(2,2),(3,3) -- empty at EOL once."""
    data = b"bab"
    got = spans(compile_pattern(r"a*"), data)
    expected = [(m.start(), m.end()) for m in stdlib_re.finditer(r"a*", "bab")]
    assert got == expected == [(0, 0), (1, 2), (2, 2), (3, 3)]
    run_logger.check(
        request.node.nodeid,
        "a* zero-width sequence",
        expected=expected,
        actual=got,
        passed=got == expected,
        reason="empty match advances one codepoint; empty match at EOF fires once",
    )


def test_zero_width_does_not_restart_inside_multibyte(run_logger, request):
    # x* where x never matches, over a multibyte buffer: one empty match per
    # CODEPOINT boundary, never at continuation bytes.
    text = "a€b"
    data = text.encode()  # 5 bytes, 3 codepoints
    got = spans(compile_pattern(r"z*"), data)
    expected = [(m.start(), m.end()) for m in stdlib_re.finditer(r"z*", text)]
    # stdlib gives char offsets; map to byte offsets
    pref = [0]
    for ch in text:
        pref.append(pref[-1] + len(ch.encode()))
    expected_bytes = [(pref[s], pref[e]) for s, e in expected]
    assert got == expected_bytes == [(0, 0), (1, 1), (4, 4), (5, 5)]
    assert all(s not in (2, 3) for s, _ in got)  # never inside €
    run_logger.check(
        request.node.nodeid,
        "zero-width codepoint alignment",
        expected=expected_bytes,
        actual=got,
        passed=got == expected_bytes,
        reason="after an empty match the scanner skips the whole € (3 bytes)",
        intermediate={"byte_starts": list(ByteIndex(data).starts)},
    )


def test_word_boundaries_ascii_agree_with_stdlib():
    text = "a b c"
    data = text.encode()
    got = spans(compile_pattern(r"\b"), data)
    expected = [m.span() for m in stdlib_re.finditer(r"\b", text)]
    assert got == expected


def test_word_boundaries_multibyte_are_ascii_scoped_and_aligned():
    # RE2 \b is ASCII-word scoped (documented difference from stdlib Unicode).
    # Assert RE2 positions directly: boundaries flank the ASCII runs only, and
    # every position is a UTF-8 codepoint boundary.
    from app.textspec import ByteIndex

    text = "a€b a€b"
    data = text.encode()
    idx = ByteIndex(data)
    got = spans(compile_pattern(r"\b"), data)
    assert got == [(0, 0), (1, 1), (4, 4), (5, 5), (6, 6), (7, 7), (10, 10), (11, 11)]
    assert all(idx.is_aligned(s) and idx.is_aligned(e) for s, e in got)


def test_adjacent_non_empty_matches_are_both_emitted():
    # a+ on 'aaaa' -> one match (0,4); the pattern 'a' instead gives four
    # adjacent single-byte matches with no gap between them.
    got = spans(compile_pattern(r"a"), b"aaaa")
    assert got == [(0, 1), (1, 2), (2, 3), (3, 4)]


def test_match_end_can_be_next_match_start():
    # 'ab' pairs on 'abab'
    got = spans(compile_pattern(r"ab"), b"abab")
    assert got == [(0, 2), (2, 4)]


def test_byte_offsets_are_raw_utf8_for_multibyte_match():
    data = "aa世界bb".encode()
    cands = scan(compile_pattern(r"世界"), data, ByteIndex(data))
    assert [(c.start, c.end) for c in cands] == [(2, 8)]
    assert cands[0].group(0).value == "世界".encode()


def test_optional_unmatched_group_is_distinguished():
    cands = scan(compile_pattern(r"(a)(b)?"), b"a")
    (c,) = cands
    assert c.group(1).value == b"a"
    assert c.group(2).value is None
    assert c.group(2).start is None and c.group(2).end is None


def test_lookahead_is_rejected_as_unsupported():
    with pytest.raises(UnsupportedSyntaxError) as exc:
        compile_pattern(r"(?=x)")
    assert exc.value.code == "unsupported_syntax"


def test_invalid_pattern_is_input_error_code():
    from app.errors import InvalidPatternError

    with pytest.raises(InvalidPatternError) as exc:
        compile_pattern(r"(")
    assert exc.value.code == "invalid_pattern"
    assert "engine_message" in exc.value.details


def test_max_mem_budget_is_separable_category():
    with pytest.raises(PatternBudgetExceededError) as exc:
        compile_pattern(r"[a-z]+(?:[0-9]*)*", EngineOptions(max_mem=64))
    assert exc.value.code == "pattern_budget_exceeded"
    assert exc.value.category == "RESOURCE"


def test_flag_m_opens_line_anchors():
    data = b"ab\ncd"
    assert spans(compile_pattern(r"^.", EngineOptions(flags="m")), data) == [(0, 1), (3, 4)]
    # without m, only text start
    assert spans(compile_pattern(r"^."), data) == [(0, 1)]


def test_match_budget_cap_on_scanner():
    from app.engine.scanner import _BudgetOverflow

    # z* never consumes input -> one empty candidate per codepoint plus the
    # terminal one: 101 for 100 bytes, so a cap of 3 trips on the 4th.
    with pytest.raises(_BudgetOverflow):
        scan(compile_pattern(r"z*"), b"a" * 100, ByteIndex(b"a" * 100), max_matches=3)
    # exactly at the cap returns that many (first candidate beyond trips)
    out = scan(compile_pattern(r"a"), b"aaa", ByteIndex(b"aaa"), max_matches=3)
    assert len(out) == 3
    with pytest.raises(_BudgetOverflow):
        scan(compile_pattern(r"a"), b"aaa", ByteIndex(b"aaa"), max_matches=2)
