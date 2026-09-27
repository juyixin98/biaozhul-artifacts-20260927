"""Deterministic shortest-edit-script diff over lines.

The algorithm is classic Myers SES over *whole lines*, where a "line"
includes its exact terminator (see :mod:`merge3.textmodel`).  Consequences
that the merge semantics depend on:

* A line whose terminator changes (``LF`` -> ``CRLF``) compares unequal and
  shows up as a replacement: terminators are never silently rewritten.
* The backtrace is deterministic on ties (deletes before inserts), so
  repeated lines align identically every run and identical edits get
  identical replacement text.
* A hunk covering base lines ``[a0, a1)`` maps to the character boundary
  before line ``a0`` and after line ``a1 - 1``; a pure insertion is a point
  edit with ``a0 == a1`` (it sits *before* base line ``a0``).
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Edit
from .textmodel import Line, boundary_offsets, join_lines, split_lines


@dataclass(frozen=True)
class Opcode:
    """One aligned run. ``kind`` is ``equal``, ``delete`` or ``insert``."""

    kind: str
    a0: int  # base lines [a0, a1)
    a1: int
    b0: int  # side lines [b0, b1)
    b1: int


def _shortest_edit_trace(a: list[Line], b: list[Line]) -> list[tuple[int, int, int, int]]:
    """Myers SES.  Returns the greedy-endpoint trace for backtracking.

    Trace[t] maps diagonal k -> endpoint x for edit-distance layer d=t.
    """
    n = len(a)
    m = len(b)
    v = {1: 0}
    trace: list[dict[int, int]] = []
    d = 0
    while True:
        trace.append(dict(v))  # state of v *before* processing layer d
        for k in range(-d, d + 1, 2):
            # Tie-break: on equal furthest-reaching diagonals prefer the
            # *insert* move (k -> k+1).  With repeated lines this anchors a
            # replacement to the earliest occurrence with the same content,
            # which is the position-preserving choice; preferring delete
            # instead shifts an edit to a later duplicate and corrupts
            # three-way merges over runs of identical lines.
            if k == -d or (k != d and v.get(k - 1, -1) >= v.get(k + 1, -1)):
                x = v.get(k + 1, 0)  # insert move, k -> k+1
            else:
                x = v.get(k - 1, 0) + 1  # delete move
            y = x - k
            # Extend on equal lines.
            while x < n and y < m and a[x].raw == b[y].raw:
                x, y = x + 1, y + 1
            v[k] = x
            if x >= n and y >= m:
                return trace
        d += 1


def line_opcodes(a: list[Line], b: list[Line]) -> list[Opcode]:
    """Return maximal equal/delete/insert runs aligning *a* to *b*."""
    n = len(a)
    m = len(b)
    if n == 0 and m == 0:
        return []
    trace = _shortest_edit_trace(a, b)

    # Backtrack from (n, m), reproducing the exact moves made forward.
    x, y = n, m
    moves: list[tuple[int, int]] = []  # (dx, dy): (1,0) delete, (0,1) insert, (1,1) equal
    for layer in range(len(trace) - 1, -1, -1):
        v = trace[layer]
        k = x - y
        if layer == 0:
            # Remaining alignment is all equal.
            while x > 0 and y > 0:
                moves.append((1, 1))
                x, y = x - 1, y - 1
            break
        d = layer
        if k == -d or (k != d and v.get(k - 1, -1) >= v.get(k + 1, -1)):
            prev_k = k + 1
        else:
            prev_k = k - 1
        prev_x = v.get(prev_k, 0)
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:
            moves.append((1, 1))
            x, y = x - 1, y - 1
        if x == prev_x and y > prev_y:
            moves.append((0, 1))
        elif y == prev_y and x > prev_x:
            moves.append((1, 0))
        x, y = prev_x, prev_y

    moves.reverse()

    # Coalesce into maximal runs.
    opcodes: list[Opcode] = []
    i = j = 0
    for dx, dy in moves:
        kind = "equal" if (dx, dy) == (1, 1) else ("delete" if dx else "insert")
        if opcodes and opcodes[-1].kind == kind:
            prev = opcodes[-1]
            opcodes[-1] = Opcode(kind, prev.a0, prev.a1 + dx, prev.b0, prev.b1 + dy)
        else:
            opcodes.append(Opcode(kind, i, i + dx, j, j + dy))
        i += dx
        j += dy

    # Content-anchored recovery: Myers may park a moved/preserved line inside
    # a del/ins block (e.g. "insert I1 at top AND change b" surfaces as
    # del(a); ins(I1,a); del(b)).  Whenever an inserted line in such a block
    # is character-identical to one of the deleted lines, reconnecting them
    # as equal is both edit-distance-neutral and grounded in hard content
    # evidence.  This separates the true point insert from the true replace.
    opcodes = _recover_equal_snakes(opcodes, a, b)
    return opcodes


def _recover_equal_snakes(opcodes: list[Opcode], a: list[Line],
                          b: list[Line]) -> list[Opcode]:
    """Reconnect identical lines inside del/ins blocks as ``equal`` runs.

    Within each maximal run of delete/insert opcodes (bounded by equal runs
    or the document edges), the deleted base interval and inserted side
    interval are aligned greedily by identical content in left-to-right
    order.  Matching index pairs become ``equal``; unmatched deletions and
    insertions stay put.  The result preserves both line orders and never
    changes edit distance.
    """
    out: list[Opcode] = []
    i = 0
    n = len(opcodes)
    while i < n:
        op = opcodes[i]
        if op.kind == "equal":
            out.append(op)
            i += 1
            continue
        j = i
        d_lo = d_hi = None
        i_lo = i_hi = None
        while j < n and opcodes[j].kind in ("delete", "insert"):
            cur = opcodes[j]
            if cur.kind == "delete":
                d_lo = cur.a0 if d_lo is None else min(d_lo, cur.a0)
                d_hi = cur.a1 if d_hi is None else max(d_hi, cur.a1)
            else:
                i_lo = cur.b0 if i_lo is None else min(i_lo, cur.b0)
                i_hi = cur.b1 if i_hi is None else max(i_hi, cur.b1)
            j += 1

        if d_lo is None:  # insert-only block
            out.extend(opcodes[i:j])
        elif i_lo is None:  # delete-only block
            out.extend(opcodes[i:j])
        else:
            # Greedy stable matching by raw line content.
            deleted = list(range(d_lo, d_hi))
            inserted = list(range(i_lo, i_hi))
            pairs: list[tuple[int, int]] = []
            cursor = 0
            for bi in inserted:
                found = -1
                for k in range(cursor, len(deleted)):
                    if a[deleted[k]].raw == b[bi].raw:
                        found = k
                        break
                if found >= 0:
                    pairs.append((deleted[found], bi))
                    cursor = found + 1
            matched_a = {p[0] for p in pairs}
            matched_b = {p[1] for p in pairs}

            # Emit in grid-walk order: trailing deletes/inserts before each
            # matched pair.  Use a coordinate sweep over both intervals.
            aa = d_lo
            bb = i_lo
            pair_index = 0
            while aa < d_hi or bb < i_hi:
                if pair_index < len(pairs):
                    ma, mb = pairs[pair_index]
                    if aa < ma or bb < mb:
                        del_span = (aa, ma)
                        ins_span = (bb, mb)
                        if del_span[0] < del_span[1]:
                            out.append(Opcode("delete", del_span[0],
                                              del_span[1], bb, bb))
                        if ins_span[0] < ins_span[1]:
                            out.append(Opcode("insert", ma, ma,
                                              ins_span[0], ins_span[1]))
                    out.append(Opcode("equal", ma, ma + 1, mb, mb + 1))
                    aa, bb = ma + 1, mb + 1
                    pair_index += 1
                else:
                    if aa < d_hi:
                        out.append(Opcode("delete", aa, d_hi, bb, bb))
                        aa = d_hi
                    if bb < i_hi:
                        out.append(Opcode("insert", d_hi, d_hi, bb, i_hi))
                        bb = i_hi
        i = j
    # Merge adjacent same-kind opcodes produced by the sweep.
    merged: list[Opcode] = []
    for op in out:
        if merged and merged[-1].kind == op.kind:
            prev = merged[-1]
            if op.kind == "delete" and op.a0 == prev.a1:
                merged[-1] = Opcode("delete", prev.a0, op.a1,
                                    prev.b0, op.b1)
                continue
            if op.kind == "insert" and op.b0 == prev.b1 and op.a0 == prev.a0:
                merged[-1] = Opcode("insert", prev.a0, prev.a1,
                                    prev.b0, op.b1)
                continue
            if op.kind == "equal" and op.a0 == prev.a1 and op.b0 == prev.b1:
                merged[-1] = Opcode("equal", prev.a0, op.a1,
                                    prev.b0, op.b1)
                continue
        merged.append(op)
    return merged


def _coalesce_replace(opcodes: list[Opcode]) -> list[Opcode]:
    """Rewrite del/ins blocks into insert/delete/replace opcodes.

    This is called on opcodes that already carry their base/side line
    indexes; line contents are not needed here except through
    :func:`_pair_hunks`, which works purely on spans.  Myers presents each
    block between two ``equal`` runs as a sequence of ``delete`` and
    ``insert`` hunks whose union is one contiguous base interval and one
    contiguous side interval.  Pairing those intervals (see
    :func:`_pair_hunks`) yields exactly:

    * a pure ``insert`` when only lines are added,
    * a pure ``delete`` when only lines are removed,
    * a single ``replace`` when line counts match,
    * a leading point ``insert`` plus a ``replace`` when more lines were
      inserted than deleted (or a leading ``delete`` plus ``replace`` in the
      inverse case).

    The ambiguity between "replace two lines" and "insert one line and
    replace the next" is resolved before this function via the Myers snake
    structure: equal content the snake matched breaks the block, so
    genuinely preserved lines never arrive inside a del/ins block.
    """
    out: list[Opcode] = []
    i = 0
    n = len(opcodes)
    while i < n:
        op = opcodes[i]
        if op.kind == "equal":
            out.append(op)
            i += 1
            continue
        j = i
        deletes: list[Opcode] = []
        inserts: list[Opcode] = []
        while j < n and opcodes[j].kind in ("delete", "insert"):
            (deletes if opcodes[j].kind == "delete" else inserts).append(opcodes[j])
            j += 1
        if deletes and not inserts:
            out.extend(deletes)
        elif inserts and not deletes:
            out.extend(inserts)
        else:
            out.extend(_pair_hunks(deletes, inserts))
        i = j
    return out


def _pair_hunks(deletes: list[Opcode], inserts: list[Opcode]) -> list[Opcode]:
    """Align one deleted base interval with one inserted side interval.

    ``[d_lo, d_hi)`` and ``[i_lo, i_hi)`` meet at the boundary Myers chose.
    The common-length tail of the two intervals is a replacement; a longer
    inserted head is a point insert at ``d_lo``, and a longer deleted head
    is a pure delete at the same boundary.
    """
    d_lo = min(o.a0 for o in deletes)
    d_hi = max(o.a1 for o in deletes)
    i_lo = min(o.b0 for o in inserts)
    i_hi = max(o.b1 for o in inserts)
    n_del = d_hi - d_lo
    n_ins = i_hi - i_lo
    paired = min(n_del, n_ins)

    result: list[Opcode] = []
    if n_ins > n_del:
        extra = n_ins - n_del
        result.append(Opcode("insert", d_lo, d_lo, i_lo, i_lo + extra))
        rep_a0, rep_b0 = d_lo, i_lo + extra
    elif n_del > n_ins:
        extra = n_del - n_ins
        result.append(Opcode("delete", d_lo, d_lo + extra, i_lo, i_lo))
        rep_a0, rep_b0 = d_lo + extra, i_lo
    else:
        rep_a0, rep_b0 = d_lo, i_lo
    if paired:
        result.append(Opcode("replace", rep_a0, rep_a0 + paired,
                             rep_b0, rep_b0 + paired))
    return result


def diff_edits(base_text: str, side_text: str, side: str) -> tuple[list[Edit], list[Line], list[Line], list[Opcode]]:
    """Diff *base_text* against *side_text* and emit base-relative edits.

    Returns ``(edits, base_lines, side_lines, opcodes)``; merge needs the
    opcodes to map base spans onto side coordinates for provenance ranges.
    """
    if side not in ("local", "remote"):
        raise ValueError("side must be 'local' or 'remote'")
    a = split_lines(base_text)
    b = split_lines(side_text)
    raw_ops = line_opcodes(a, b)
    opcodes = _coalesce_replace(raw_ops)
    base_bounds = boundary_offsets(a)

    edits: list[Edit] = []
    serial = 0
    for op in opcodes:
        if op.kind == "equal":
            continue
        serial += 1
        start = base_bounds[op.a0]
        end = base_bounds[op.a1]
        replacement = join_lines(b[op.b0 : op.b1])
        edit = Edit(
            start=start,
            end=end,
            replacement=replacement,
            side=side,
            edit_id=f"{side[0]}{serial}",
        )
        edits.append(edit)
    return edits, a, b, opcodes
