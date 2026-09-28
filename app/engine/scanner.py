"""Codepoint-aware, zero-width-safe leftmost scanner over RE2.

Rule summary (identical semantics to Python ``re.finditer`` over decoded text,
which the test oracle uses; spans, however, are raw UTF-8 byte offsets)::

    pos = 0
    while pos <= n:
        m = pattern.search(data, pos=pos)          # leftmost match at/after pos
        if no match: stop
        emit candidate (m.start, m.end, groups)
        if m.end == m.start:                        # zero-width match
            if m.end == n: stop                     # empty match at EOF once
            pos = next codepoint boundary > m.end   # skip ONE codepoint
        else:
            pos = m.end                             # non-empty: resume after it

Because the restart position only ever moves to RE2 match ends (which align to
UTF-8 boundaries -- RE2 never returns a match split across a codepoint) or to a
whole-codepoint boundary, and because ``pattern.search`` is anchored nowhere,
matches that begin *inside* a codepoint can never be produced.  Replacement
text is never fed back through the scan: scanning is a pure function of the
original buffer (see :mod:`app.planner`).

"Adjacent" matches therefore work naturally: a match ending at ``k`` allows
the next to start at ``k``; no input byte is consumed twice because every
non-empty candidate begins at the previous candidate's end or later.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..textspec import ByteIndex, next_codepoint_boundary
from .compiler import CompiledPattern


@dataclass(frozen=True, slots=True)
class GroupSpan:
    """One capture group's byte span and bytes, or ``None`` when unmatched."""

    number: int
    start: int | None
    end: int | None
    value: bytes | None


@dataclass(frozen=True, slots=True)
class Candidate:
    """A single leftmost match of one rule against the original buffer."""

    start: int
    end: int
    groups: tuple[GroupSpan, ...] = field(default=())

    @property
    def zero_width(self) -> bool:
        return self.start == self.end

    def group(self, number: int) -> GroupSpan:
        return self.groups[number]


def _read_groups(compiled: CompiledPattern, match) -> tuple[GroupSpan, ...]:
    out: list[GroupSpan] = []
    text = match.string  # bytes
    for number in range(compiled.ngroups + 1):
        s, e = match.span(number)
        if s < 0 or e < 0:
            out.append(GroupSpan(number, None, None, None))
        else:
            out.append(GroupSpan(number, int(s), int(e), bytes(text[s:e])))
    return tuple(out)


def scan(
    compiled: CompiledPattern,
    data: bytes,
    index: ByteIndex | None = None,
    *,
    start: int = 0,
    end: int | None = None,
    max_matches: int | None = None,
) -> list[Candidate]:
    """Return all leftmost candidates in the region [start, end).

    ``start``/``end`` must be UTF-8 aligned; the caller validates them.  An
    optional ``max_matches`` bounds pathological fan-out (e.g. ``a*`` produces
    O(n) zero-width/non-empty candidates); exceeding it raises ``ValueError``
    carrying the partial count, which the planner converts to the structured
    :class:`~app.errors.MatchBudgetExceededError`.
    """
    n = len(data)
    if end is None:
        end = n
    pos = start
    regexp = compiled.regexp
    candidates: list[Candidate] = []

    # Defensive alignment: search() is given byte offsets; ensure start points
    # at a boundary (callers already guarantee this for the whole-text case).
    if pos != start:  # pragma: no cover - kept for clarity of intent
        raise AssertionError("unreachable")

    while pos <= end:
        m = regexp.search(data, pos=pos, endpos=end)
        if m is None:
            break
        ms, me = m.span(0)
        # RE2 on bytes reports byte offsets directly and aligns to codepoints.
        cand = Candidate(int(ms), int(me), _read_groups(compiled, m))
        candidates.append(cand)
        # "At most max_matches": the first candidate beyond the cap trips.
        if max_matches is not None and len(candidates) > max_matches:
            raise _BudgetOverflow(len(candidates))

        if me == ms:
            if me == end:
                break  # one empty match at the final boundary, then stop
            nxt = (
                index.advance(me)
                if index is not None
                else next_codepoint_boundary(data, me)
            )
            if nxt is None or nxt > end:
                break
            pos = nxt
        else:
            pos = me
    return candidates


class _BudgetOverflow(Exception):
    """Internal: candidate fan-out exceeded. Planner maps to structured error."""

    def __init__(self, count: int) -> None:
        super().__init__(f"match budget exceeded at {count}")
        self.count = count
