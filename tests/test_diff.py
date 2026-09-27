"""Tests for the deterministic diff: edit shape, anchors, determinism."""

import pytest

from merge3.diff import diff_edits, line_opcodes
from merge3.model import EditKind
from merge3.textmodel import split_lines


def test_no_changes_emits_no_edits():
    edits, _, _, ops = diff_edits("a\nb\n", "a\nb\n", "local")
    assert edits == []
    assert all(op.kind == "equal" for op in ops)


def test_one_line_replacement_is_single_replace_edit():
    # Regression: a replacement must be one range edit, not a delete plus an
    # insert at the same anchor (which would hide delete-vs-modify conflicts).
    edits, a, b, ops = diff_edits("a\nb\nc\n", "a\nB\nc\n", "local")
    assert len(edits) == 1
    edit = edits[0]
    assert edit.kind == EditKind.REPLACE
    assert edit.start == 2
    assert edit.end == 4           # exactly "b\n"
    assert edit.replacement == "B\n"
    assert edit.edit_id == "l1"


def test_pure_deletion_and_pure_insertion_shapes():
    edits, *_ = diff_edits("a\nb\nc\n", "a\nc\n", "local")
    assert len(edits) == 1
    assert edits[0].kind == EditKind.DELETE
    assert edits[0].replacement == ""

    edits, *_ = diff_edits("a\nb\n", "x\na\nb\n", "local")
    assert len(edits) == 1
    assert edits[0].kind == EditKind.INSERT
    assert edits[0].start == edits[0].end == 0
    assert edits[0].replacement == "x\n"


def test_insertion_at_end_is_point_edit_at_document_length():
    edits, *_ = diff_edits("a\n", "a\nb\n", "remote")
    assert len(edits) == 1
    assert edits[0].is_point
    assert edits[0].start == len("a\n")


def test_edits_within_one_side_are_disjoint_and_sorted():
    base = "".join(f"line{i}\n" for i in range(10))
    side = "line0\nXX\nline2\nline3\nYY\nline5\nline6\nline7\nline8\nline9\n"
    edits, *_ = diff_edits(base, side, "local")
    spans = [(e.start, e.end) for e in edits]
    assert spans == sorted(spans)
    for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
        assert e1 <= s2  # half-open: abutting is still disjoint


def test_deterministic_on_repeated_lines():
    # The same repeated-line input must produce identical edits every run;
    # run several times to guard against any unordered structure.
    base = "r\nr\nr\nr\n"
    side = "r\nX\nr\nr\n"
    first = [e.replacement for e in diff_edits(base, side, "local")[0]]
    for _ in range(20):
        again = [e.replacement for e in diff_edits(base, side, "local")[0]]
        assert again == first


def test_applying_edits_reconstructs_side():
    # Independent oracle (not the merge engine): apply edits back-to-front.
    base = "l1\nl2\nl3\nl4\n"
    side = "l1\nNEW\nl3\nEXTRA\nl4\n"
    edits, *_ = diff_edits(base, side, "local")
    out = base
    for e in sorted(edits, key=lambda x: (x.start, 0 if x.is_point else 1),
                    reverse=True):
        out = out[: e.start] + e.replacement + out[e.end :]
    assert out == side


def test_invalid_side_name_rejected():
    with pytest.raises(ValueError):
        diff_edits("a\n", "b\n", "upstream")


def test_myers_moves_cover_every_position():
    # Every opcode sequence must partition both line sequences exactly.
    a = split_lines("p1\np2\np3\np4\n")
    b = split_lines("p2\np1\np3\np4\n")
    ops = line_opcodes(a, b)
    assert (sum(o.a1 - o.a0 for o in ops),
            sum(o.b1 - o.b0 for o in ops)) == (4, 4)
    # and reconstruct b when applied to a
    rebuilt = []
    for op in ops:
        rebuilt.extend(b[op.b0:op.b1] if op.kind in ("insert", "replace")
                       else [] )
        rebuilt.extend(a[op.a0:op.a1] if op.kind in ("equal", "delete")
                       else [] )
    # For replace opcodes both a- and b-lines appear above; simpler: check
    # via the edit-based reconstruction instead.
    # Rebuild from opcode semantics:
    rebuilt = []
    for op in ops:
        if op.kind == "equal":
            rebuilt.extend(a[op.a0:op.a1])
        elif op.kind in ("insert", "replace"):
            rebuilt.extend(b[op.b0:op.b1])
    assert "".join(x.raw for x in rebuilt) == "p2\np1\np3\np4\n"
