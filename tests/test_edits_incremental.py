"""Incremental edit tests.

* concrete edits on combining marks, flags, ZWJ emoji and CRLF with
  hand-verified boundary/merge expectations;
* randomized edits where the incrementally updated index must be identical
  to a fresh full rebuild (the decisive comparison);
* failure-category assertions for illegal anchors (continuation byte,
  mid-cluster codepoint, reversed range).
"""

from __future__ import annotations

import random

import pytest

from textindex import index as index_mod
from textindex.edits import Edit, apply_edit
from textindex.errors import (
    EditRangeCrossed,
    IllegalByteBoundary,
    IllegalCodepointBoundary,
    IllegalGraphemeBoundary,
)

from . import oracle

SEED = 424242


def test_insert_combining_mark_none_merges_clusters(recorder):
    rec, counts = recorder
    idx = index_mod.build_index("ex", )  # NONE semantics at index layer
    # insert combining acute after 'e' (cp 1) in NONE normalization.
    result = apply_edit(idx, Edit(1, 1, "́", "codepoint"),
                        normalization="NONE")
    expected_text = "éx"
    passed = result.text == expected_text and result.index.cluster_count == 2
    counts["PASS" if passed else "FAIL"] += 1
    rec.judge(
        test="insert_combining", kind="incremental", passed=passed,
        expected={"text": expected_text, "clusters": ["é", "x"],
                  "cp": [0, 2, 3], "byte": [0, 3, 4]},
        actual={"text": result.text,
                "clusters": result.index.clusters(),
                "cp": list(result.index.cp_start),
                "byte": list(result.index.byte_start)},
        intermediate={"window": result.new_window,
                      "rebuilt": result.rebuilt_clusters},
        reason="inserted mark must merge into preceding cluster, resegmented "
               "via window rather than character counting",
    )
    assert list(result.index.byte_start) == [0, 3, 4]


def test_insert_zwj_between_two_pictographs_merges():
    idx = index_mod.build_index("\U0001F44B\U0001F600")  # 2 clusters
    result = apply_edit(idx, Edit(1, 1, "‍", "grapheme"),
                        normalization="NONE")
    # WAVE + ZWJ + GRINNING: ZWJ joins pictographic -> single cluster
    assert result.text == "\U0001F44B‍\U0001F600"
    expected = index_mod.rebuild(result.text)
    assert list(result.index.cp_start) == list(expected.cp_start)
    assert list(result.index.byte_start) == list(expected.byte_start)
    assert result.index.cluster_count == 1


def test_ris_parity_after_insertion():
    # one flag + ascii: "US" "x" ; insert J (RIS) between flag and x.
    idx = index_mod.build_index("\U0001F1FA\U0001F1F8x")
    result = apply_edit(idx, Edit(1, 1, "\U0001F1EF", "grapheme"),
                        normalization="NONE")
    # U S J -> [U S][J]; x follows
    assert result.text == "\U0001F1FA\U0001F1F8\U0001F1EFx"
    exp = oracle.expected_index(result.text)
    assert list(result.index.cp_start) == exp["cp_starts"]
    assert list(result.index.byte_start) == exp["byte_starts"]
    assert result.index.cluster_count == 3  # [US][J][x]


def test_ris_parity_insert_before_existing_flag():
    # lone J then flag US: [J][U S]; insert S (RIS) before J -> [S J][U S]
    idx = index_mod.build_index("\U0001F1EF\U0001F1FA\U0001F1F8")
    assert idx.cluster_count == 2
    result = apply_edit(idx, Edit(0, 0, "\U0001F1F8", "grapheme"),
                        normalization="NONE")
    exp = oracle.expected_index(result.text)
    assert list(result.index.cp_start) == exp["cp_starts"]
    assert result.index.cluster_count == 2


def test_crlf_edit_does_not_split_pair():
    idx = index_mod.build_index("a\r\nb")
    assert idx.cluster_count == 3  # [a][CRLF][b]
    # deleting CR alone (cp 1..2) must be refused: cp 1 is inside CRLF
    with pytest.raises(IllegalCodepointBoundary):
        apply_edit(idx, Edit(1, 2, "", "codepoint"), normalization="NONE")
    # deleting the whole CRLF cluster [1,2) leaves b untouched
    result = apply_edit(idx, Edit(1, 2, "", "grapheme"), normalization="NONE")
    assert result.text == "ab"
    assert result.index.cluster_count == 2


def test_byte_anchor_inside_multibyte_rejected():
    idx = index_mod.build_index("é")  # C3 A9
    with pytest.raises(IllegalByteBoundary):
        apply_edit(idx, Edit(1, 1, "x", "byte"), normalization="NONE")


def test_byte_anchor_at_cluster_boundary_accepted():
    idx = index_mod.build_index("éx")
    result = apply_edit(idx, Edit(2, 2, "z", "byte"), normalization="NONE")
    assert result.text == "ézx"


def test_reversed_range_and_out_of_range():
    idx = index_mod.build_index("abc")
    with pytest.raises(EditRangeCrossed):
        Edit(2, 1, "", "grapheme")
    # end beyond the legal end sentinel (3 clusters -> max index 3)
    with pytest.raises(IllegalGraphemeBoundary):
        apply_edit(idx, Edit(0, 9, "", "grapheme"), normalization="NONE")


def test_edit_at_document_ends():
    idx = index_mod.build_index("\U0001F600")
    head = apply_edit(idx, Edit(0, 0, "a", "grapheme"), normalization="NONE")
    assert head.text == "a\U0001F600"
    tail = apply_edit(idx, Edit(1, 1, "z", "grapheme"), normalization="NONE")
    assert tail.text == "\U0001F600z"
    empty_out = apply_edit(idx, Edit(0, 1, "", "grapheme"),
                           normalization="NONE")
    assert empty_out.text == ""
    assert empty_out.index.cluster_count == 0


@pytest.mark.parametrize("trial", range(150))
def test_incremental_equals_full_rebuild_random(trial):
    rng = random.Random(SEED + trial)
    text = oracle.random_text(rng, min_len=1, max_len=45)
    idx = index_mod.build_index(text)
    s_cp, e_cp, replacement = oracle.random_cluster_anchored_edit(rng, text)
    form = "NONE"
    # oracle computes the new text independently
    new_text = oracle.normalize_edit(text, s_cp, e_cp, replacement, form)
    result = apply_edit(idx, Edit(s_cp, e_cp, replacement, "codepoint"),
                        normalization=form)
    full = index_mod.rebuild(new_text)
    assert result.text == new_text
    assert list(result.index.cp_start) == list(full.cp_start), (
        f"trial {trial}: {text!r} -> {new_text!r}")
    assert list(result.index.byte_start) == list(full.byte_start)
    exp = oracle.expected_index(new_text)
    assert list(result.index.cp_start) == exp["cp_starts"]
    assert list(result.index.byte_start) == exp["byte_starts"]


@pytest.mark.parametrize("trial", range(80))
def test_incremental_nfc_equals_full_rebuild_random(trial):
    rng = random.Random(SEED * 7 + trial)
    text = oracle.normalize(oracle.random_text(rng, 1, 40), "NFC")
    idx = index_mod.build_index(text)
    s_cp, e_cp, replacement = oracle.random_cluster_anchored_edit(rng, text)
    new_text = oracle.normalize_edit(text, s_cp, e_cp, replacement, "NFC")
    result = apply_edit(idx, Edit(s_cp, e_cp, replacement, "codepoint"),
                        normalization="NFC")
    full = index_mod.rebuild(new_text)
    assert result.text == new_text
    assert list(result.index.cp_start) == list(full.cp_start)
    assert list(result.index.byte_start) == list(full.byte_start)


def test_window_reports_diagnostics():
    idx = index_mod.build_index("abcdef")
    result = apply_edit(idx, Edit(2, 3, "XY", "grapheme"),
                        normalization="NONE")
    assert result.reused_before >= 1 and result.reused_after >= 1
    assert result.rebuilt_clusters >= 1
    assert result.delta_codepoints == 1
