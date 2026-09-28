"""Segmentation + bidirectional index tests against hand constants and the
independent regex ``\\X`` oracle."""

from __future__ import annotations

import pytest

from textindex import index as index_mod
from textindex import segmenter
from textindex.errors import (
    CATEGORY_INPUT_ERROR,
    IllegalByteBoundary,
    IllegalCodepointBoundary,
    IllegalGraphemeBoundary,
    PositionOutOfRange,
)

from . import fixtures, oracle


@pytest.mark.parametrize("key", list(fixtures.ALL_TEXT_FIXTURES))
def test_fixtures_match_hand_computed_tables(key, recorder):
    rec, counts = recorder
    text, expected = fixtures.ALL_TEXT_FIXTURES[key]
    idx = index_mod.build_index(text)
    actual = {
        "codepoints": idx.codepoint_count,
        "bytes": idx.byte_count,
        "clusters": idx.clusters(),
        "cluster_cp_starts": list(idx.cp_start),
        "cluster_byte_starts": list(idx.byte_start),
    }
    passed = actual == expected
    counts["PASS" if passed else "FAIL"] += 1
    rec.judge(
        test=f"fixture_{key}", kind="hand_constant", passed=passed,
        expected=expected, actual=actual,
        intermediate={"text_codepoints": [f"U+{ord(c):04X}" for c in text][:20]},
        reason="built index must equal the hand-computed offset tables",
    )
    assert passed, f"fixture {key} mismatch"


@pytest.mark.parametrize("key", list(fixtures.ALL_TEXT_FIXTURES))
def test_library_agrees_with_regex_oracle(key):
    text, _ = fixtures.ALL_TEXT_FIXTURES[key]
    assert segmenter.cluster_spans(text) == oracle.cluster_spans(text)


def test_combining_mark_is_not_a_boundary():
    # "é" = one cluster; bytes: 'e'=0, mark CC 81 at 1..3.
    idx = index_mod.build_index("é")
    assert idx.cluster_count == 1
    with pytest.raises(IllegalCodepointBoundary) as ei:
        idx.codepoint_to_cluster(1, strict=True)
    assert ei.value.category == CATEGORY_INPUT_ERROR
    # byte 1 is the mark's lead byte (a codepoint start) but not a cluster
    # boundary -> illegal codepoint boundary; byte 2 is a continuation byte
    # -> the stricter illegal *byte* boundary.
    with pytest.raises(IllegalCodepointBoundary):
        idx.byte_to_cluster(1, strict=True)
    with pytest.raises(IllegalByteBoundary):
        idx.byte_to_cluster(2, strict=True)
    with pytest.raises(IllegalByteBoundary):
        idx.byte_to_codepoint(2, strict=False)


def test_zwj_sequence_internals_rejected():
    # MAN ZWJ WOMAN ZWJ GIRL = 5 codepoints, one cluster (fixture D).
    idx = index_mod.build_index(
        "\U0001F468‍\U0001F469‍\U0001F467")
    assert idx.cluster_count == 1
    assert idx.byte_count == 18
    # every internal codepoint (1..4) is an illegal edit anchor
    for cp in (1, 2, 3, 4):
        with pytest.raises(IllegalCodepointBoundary):
            idx.codepoint_to_cluster(cp, strict=True)
    # MAN = F0 9F 98 A8 at bytes 0..3; first ZWJ = E2 80 8D at bytes 4..6.
    with pytest.raises(IllegalByteBoundary):
        idx.byte_to_cluster(1, strict=True)   # continuation of MAN
    with pytest.raises(IllegalByteBoundary):
        idx.byte_to_cluster(5, strict=True)   # continuation of first ZWJ
    with pytest.raises(IllegalCodepointBoundary):
        idx.byte_to_cluster(4, strict=True)   # lead byte of first ZWJ
    with pytest.raises(IllegalCodepointBoundary):
        idx.byte_to_cluster(7, strict=True)   # lead byte of WOMAN


def test_ris_pairing_boundaries():
    # three RIS: boundary after cp2 is legal; after cp1 is inside a flag.
    idx = index_mod.build_index("\U0001F1FA\U0001F1F8\U0001F1EF")
    assert idx.codepoint_to_cluster(2, strict=True) == 1
    with pytest.raises(IllegalCodepointBoundary):
        idx.codepoint_to_cluster(1, strict=True)


def test_crlf_is_one_cluster():
    idx = index_mod.build_index("\r\n")
    assert idx.cluster_count == 1
    assert list(idx.cp_start) == [0, 2]
    with pytest.raises(IllegalCodepointBoundary):
        idx.codepoint_to_cluster(1, strict=True)


def test_position_out_of_range_distinct_from_illegal_boundary():
    idx = index_mod.build_index("abc")
    # N itself (3) is the legal end sentinel; N+1 is out of range.
    for bad, unit in [(4, "codepoint"), (4, "byte")]:
        with pytest.raises(PositionOutOfRange) as ei:
            if unit == "codepoint":
                idx.codepoint_to_cluster(bad)
            else:
                idx.byte_to_cluster(bad)
        assert ei.value.code == "position_out_of_range"
    # A too-large *cluster* index is the dedicated illegal-grapheme class.
    with pytest.raises(IllegalGraphemeBoundary):
        idx.cluster_to_codepoint(99)


def test_nonstrict_containing_cluster_for_internal_offsets():
    idx = index_mod.build_index("éz")
    assert idx.byte_to_cluster(1, strict=False) == 0  # mark inside cluster 0
    assert idx.codepoint_to_cluster(1, strict=False) == 0
    assert idx.byte_to_codepoint(1, strict=False) == 1
    assert idx.codepoint_to_byte(1, strict=False) == 1


@pytest.mark.parametrize("key", list(fixtures.ALL_TEXT_FIXTURES))
def test_roundtrip_boundary_tables_consistent(key):
    text, _ = fixtures.ALL_TEXT_FIXTURES[key]
    idx = index_mod.build_index(text)
    # every cluster start maps identically through all three spaces
    for k in range(idx.cluster_count + 1):
        cp = idx.cluster_to_codepoint(k)
        by = idx.cluster_to_byte(k)
        assert idx.codepoint_to_cluster(cp, strict=True) == k
        assert idx.byte_to_cluster(by, strict=True) == k
        assert idx.byte_to_codepoint(by, strict=True) == cp
        assert idx.codepoint_to_byte(cp, strict=True) == by
