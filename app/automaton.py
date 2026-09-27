"""Aho-Corasick automaton over **bytes**.

Responsibilities of this module:

* compile an ordered set of byte patterns into a trie;
* compute failure links (BFS) and *output/dictionary* links so that suffix
  patterns are never missed;
* drive one state through a byte stream and report every hit as
  ``(start_offset, end_offset, pattern_id)`` with offsets relative to the
  start of the *whole logical stream* (the caller supplies the base offset,
  which makes cross-chunk positions exact).

Non-goals (deliberately kept elsewhere): normalization (:mod:`app.spec`),
stream lifecycle/pagination (:mod:`app.matcher`, :mod:`app.services.scans`),
persistence (:mod:`app.storage`).

A compiled automaton is immutable. A scan created against version X holds the
exact ``Automaton`` instance it was compiled from; switching versions requires
an explicit reset that builds a *new* matcher — node ids from different
automata are never mixed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Hit = Tuple[int, int, int]  # (start_inclusive, end_exclusive, pattern_id)


@dataclass
class _Node:
    next: Dict[int, int] = field(default_factory=dict)
    fail: int = 0
    # dictionary/output link: nearest failure-chain ancestor that is terminal
    # (0 when none). Traversing this chain enumerates every suffix pattern.
    output: int = 0
    # pattern ids that terminate exactly at this node (normally one; the
    # builder rejects duplicates, but the list keeps traversal uniform).
    pattern_ids: List[int] = field(default_factory=list)


class AutomatonBuildError(ValueError):
    """Raised for pattern sets that cannot define an automaton."""


class Automaton:
    """An immutable compiled automaton.

    Pattern ids are positions in ``patterns`` (0-based, insertion order).
    """

    def __init__(self, patterns: Sequence[bytes]):
        if not patterns:
            raise AutomatonBuildError(
                "an automaton requires at least one pattern; an empty pattern "
                "set would silently match nothing"
            )
        seen: Dict[bytes, int] = {}
        for idx, p in enumerate(patterns):
            if not isinstance(p, (bytes, bytearray, memoryview)):
                raise AutomatonBuildError(
                    f"pattern {idx} is {type(p).__name__}, expected bytes"
                )
            if len(p) == 0:
                # Belt and braces: the version service is expected to reject
                # empty patterns first with EmptyPatternError.
                raise AutomatonBuildError(f"pattern at index {idx} is empty")
            pb = bytes(p)
            if pb in seen:
                raise AutomatonBuildError(
                    f"duplicate pattern at index {idx} "
                    f"(identical to index {seen[pb]})"
                )
            seen[pb] = idx

        self.patterns: Tuple[bytes, ...] = tuple(bytes(p) for p in patterns)
        self._lengths: Tuple[int, ...] = tuple(len(p) for p in self.patterns)
        self._nodes: List[_Node] = [_Node()]
        self._build_trie()
        self._build_links()

    # ---- construction -------------------------------------------------------

    def _build_trie(self) -> None:
        for pid, pattern in enumerate(self.patterns):
            state = 0
            for b in pattern:
                nxt = self._nodes[state].next.get(b)
                if nxt is None:
                    nxt = len(self._nodes)
                    self._nodes[state].next[b] = nxt
                    self._nodes.append(_Node())
                state = nxt
            self._nodes[state].pattern_ids.append(pid)

    def _build_links(self) -> None:
        """BFS failure-link + output-link construction."""
        queue: List[int] = []
        # Root's direct children fail at root.
        for child in self._nodes[0].next.values():
            self._nodes[child].fail = 0
            queue.append(child)

        head = 0
        while head < len(queue):
            u = queue[head]
            head += 1
            node_u = self._nodes[u]
            for byte, v in node_u.next.items():
                f = node_u.fail
                # Walk failure links until a transition on `byte` exists.
                while f != 0 and byte not in self._nodes[f].next:
                    f = self._nodes[f].fail
                child_fail = self._nodes[f].next.get(byte, 0)
                # u -> v is a real trie edge; never point a node at itself.
                if child_fail == v:
                    child_fail = 0
                self._nodes[v].fail = child_fail

                fail_node = self._nodes[child_fail]
                if fail_node.pattern_ids:
                    self._nodes[v].output = child_fail
                else:
                    self._nodes[v].output = fail_node.output
                queue.append(v)

    # ---- introspection ------------------------------------------------------

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def pattern_count(self) -> int:
        return len(self.patterns)

    def pattern_length(self, pattern_id: int) -> int:
        return self._lengths[pattern_id]

    def root(self) -> int:
        return 0

    # ---- matching -----------------------------------------------------------

    def transition(self, state: int, byte: int) -> int:
        """One deterministic step (visible for the matcher and for tests)."""
        s = state
        while s != 0 and byte not in self._nodes[s].next:
            s = self._nodes[s].fail
        return self._nodes[s].next.get(byte, 0)

    def outputs_at(self, state: int) -> Iterable[int]:
        """All pattern ids terminating at ``state`` or at a strict suffix.

        Order: the state's own terminating patterns first (ids ascending),
        then each output-link ancestor's terminating patterns (longest suffix
        chain first). This order is deterministic and is part of the contract
        used for hit row ordering.
        """
        t = state
        while t != 0:
            node = self._nodes[t]
            for pid in node.pattern_ids:
                yield pid
            t = node.output

    def feed(
        self, state: int, buf: bytes, base_offset: int
    ) -> Tuple[int, List[Hit]]:
        """Consume one contiguous buffer; return new state and emitted hits.

        ``base_offset`` is the number of stream bytes already consumed before
        ``buf``. Reported ``end`` offsets are exclusive and absolute within the
        logical stream, so concatenated chunks yield the same offsets as one
        big buffer.
        """
        hits: List[Hit] = []
        s = state
        nodes = self._nodes
        for pos, b in enumerate(buf):
            cur = s
            while cur != 0 and b not in nodes[cur].next:
                cur = nodes[cur].fail
            s = nodes[cur].next.get(b, 0)
            end = base_offset + pos + 1

            t = s
            while t != 0:
                node = nodes[t]
                for pid in node.pattern_ids:
                    hits.append((end - self._lengths[pid], end, pid))
                t = node.output
        return s, hits
