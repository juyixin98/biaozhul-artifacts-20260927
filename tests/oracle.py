"""Independent oracle: naive per-pattern byte search.

This module deliberately shares **no code** with the Aho-Corasick
implementation under test. It is the ground truth used to verify:

* no hits are missed (including suffix patterns and overlaps);
* no phantom hits are reported;
* streaming over arbitrary chunk boundaries yields exactly the same hit
  multiset as one-shot matching;
* raw byte offsets are the same regardless of chunk size.

The search uses Python's built-in ``bytes.find`` in a loop. It does not use
any automaton, failure link or output-link logic, so a bug shared with
:mod:`app.automaton` is impossible by construction.
"""
from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

Hit = Tuple[int, int, int]  # (start, end_exclusive, pattern_id)


def find_all(haystack: bytes, needle: bytes, start: int = 0) -> List[int]:
    """All occurrence start positions of ``needle`` in ``haystack``.

    Overlapping occurrences included (the ``start + 1`` advance is the whole
    point — naive ``str.replace`` style matching would hide them).
    """
    if not needle:
        raise ValueError("oracle refuses empty needles")
    positions: List[int] = []
    i = start
    while True:
        j = haystack.find(needle, i)
        if j == -1:
            break
        positions.append(j)
        i = j + 1  # overlap: advance by exactly one byte
    return positions


def naive_match(data: bytes, patterns: Sequence[bytes]) -> List[Hit]:
    """Reference matcher for one contiguous buffer."""
    hits: List[Hit] = []
    for pid, pat in enumerate(patterns):
        for start in find_all(data, pat):
            hits.append((start, start + len(pat), pid))
    # Same canonical order the service promises:
    # (end, start, pattern_id).
    hits.sort(key=lambda h: (h[1], h[0], h[2]))
    return hits


def naive_stream(
    chunks: Iterable[bytes], patterns: Sequence[bytes]
) -> List[Hit]:
    """Reference streaming: concatenate first, then match.

    Real streaming cannot look across future chunks the way this does; this is
    intentionally the *oracle*, used to judge the real engine.
    """
    return naive_match(b"".join(chunks), patterns)


def as_multiset(hits: Iterable[Hit]) -> List[Hit]:
    return sorted(hits)
