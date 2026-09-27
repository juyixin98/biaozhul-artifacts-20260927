"""Tests for text normalization: terminator and trailing-newline fidelity."""

import pytest

from merge3.textmodel import (
    Line,
    boundary_offsets,
    detect_dominant_eol,
    join_lines,
    normalize_eol,
    split_lines,
)


@pytest.mark.parametrize("text", [
    "",
    "\n",
    "\r\n",
    "\r",
    "a",
    "a\n",
    "a\r\n",
    "a\r",
    "a\nb",
    "a\nb\n",
    "a\r\nb\r\n",
    "a\rb\r",
    "a\n\nb\n",
    "mixed\r\nlone\nlast\rcrlf\r\nend",
])
def test_split_join_roundtrip(text):
    assert join_lines(split_lines(text)) == text


def test_lines_carry_their_terminators():
    lines = split_lines("a\nb\r\nc\rd")
    assert lines == [
        Line("a", "\n"),
        Line("b", "\r\n"),
        Line("c", "\r"),
        Line("d", ""),
    ]


def test_empty_document_has_no_lines_not_one_blank():
    assert split_lines("") == []


def test_boundary_offsets_point_to_exact_characters():
    text = "ab\ncde\r\n"
    lines = split_lines(text)
    bounds = boundary_offsets(lines)
    assert bounds == [0, 3, 8]
    # Slicing at consecutive boundaries reproduces the raw lines.
    for k, line in enumerate(lines):
        assert text[bounds[k]:bounds[k + 1]] == line.raw


def test_detect_dominant_eol():
    assert detect_dominant_eol("a\nb\nc\r\n") == "\n"
    assert detect_dominant_eol("a\r\nb\r\nc\n") == "\r\n"
    assert detect_dominant_eol("x") == "\n"


def test_normalize_eol_is_explicit_only():
    text = "a\rb\r\nc\nd"
    assert normalize_eol(text, "lf") == "a\nb\nc\nd"
    assert normalize_eol(text, "crlf") == "a\r\nb\r\nc\r\nd"
    assert normalize_eol(text, "cr") == "a\rb\rc\rd"
    # preserve is a true no-op even with mixed endings
    assert normalize_eol(text, "preserve") == text
    # the final unterminated line must remain unterminated after conversion
    assert not normalize_eol("a\nb", "crlf").endswith("\r\n")
    with pytest.raises(ValueError):
        normalize_eol(text, "nonsense")
