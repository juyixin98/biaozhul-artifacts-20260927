"""Unit tests for normalization and the original-offset mapping invariants."""
from __future__ import annotations

from app.core.normalize import fold_character, normalize_surface, normalize_text


def test_fullwidth_and_casefold():
    norm = normalize_text("ＡＢc")  # fullwidth A B + ascii c
    assert norm.text == "abc"
    assert norm.source_length == 3
    assert norm.removed_chars == 0


def test_ligature_expansion_owner():
    norm = normalize_text("ﬁsh")
    assert norm.text == "fish"
    assert norm.owner == (0, 0, 1, 2)  # both "f","i" owned by original char 0


def test_eszett_expansion():
    norm = normalize_text("aßb")
    assert norm.text == "assb"
    assert norm.owner == (0, 1, 1, 2)


def test_zero_width_space_removed():
    assert fold_character("​") == ""
    norm = normalize_text("a​b")
    assert norm.text == "ab"
    assert norm.removed_chars == 1


def test_soft_hyphen_removed():
    norm = normalize_text("x­y")
    assert norm.text == "xy"
    assert norm.removed_chars == 1


def test_spans_tile_for_expansion():
    source = "aßb"
    norm = normalize_text(source)
    pieces = []
    cursor = 0
    for i in range(1, len(norm.text) + 1):
        s, e = norm.orig_span(i - 1, i)
        assert s == cursor
        pieces.append(source[s:e])
        cursor = e
    assert cursor == len(source)
    assert "".join(pieces) == source


def test_spans_tile_with_removed_chars():
    source = "研​生命"
    norm = normalize_text(source)
    rebuilt = ""
    cursor = 0
    for i in range(1, len(norm.text) + 1):
        s, e = norm.orig_span(i - 1, i)
        assert s == cursor
        rebuilt += source[s:e]
        cursor = e
    assert cursor == len(source)
    assert rebuilt == source


def test_format_only_input_spans_whole_source():
    source = "​­"
    norm = normalize_text(source)
    assert norm.text == ""
    # Even with no surviving chars the helper keeps the range valid.
    assert norm.orig_span(0, 0) == (0, 2)


def test_empty_input():
    norm = normalize_text("")
    assert norm.text == ""
    assert norm.orig_span(0, 0) == (0, 0)


def test_normalize_surface_matches_text_policy():
    assert normalize_surface("ＳTRASSE") == "strasse"
    assert normalize_surface("ﬁ") == "fi"


def test_consecutive_removed_run_attaches_forward():
    source = "a​­b"  # a, ZWSP, SHY, b
    norm = normalize_text(source)
    assert norm.text == "ab"
    # Spans of the two surviving tokens cover all four original chars.
    assert norm.orig_span(0, 1) == (0, 1)
    assert norm.orig_span(1, 2) == (1, 4)
