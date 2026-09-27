"""Streaming matcher: one open scan's stateful engine.

Binds together:

* one immutable :class:`~app.automaton.Automaton` (a *specific* compiled
  pattern version),
* one :class:`~app.spec.TextSpec` plus its incremental
  :class:`~app.spec.StreamingTextDecoder`,
* mutable streaming state (current automaton node, bytes consumed, epoch).

The state machine is byte oriented. ``push`` is the only way to advance it;
``reset`` is the only way to change the pattern set and it rebuilds the engine
from a freshly compiled automaton. It is therefore impossible to drive a node
id from version A through automaton B — the object you get after a reset does
not even share the automaton reference.

Byte offsets across chunks and multibyte boundaries
---------------------------------------------------
The automaton consumes "released" bytes only (an unterminated UTF-8 lead at a
chunk edge is held back until its continuation arrives). Hit offsets are
nevertheless offsets into the *raw payload*: released byte ``i`` is fed with
base offset ``i``. This works because the hold-back is always a *suffix* of
the bytes accepted so far, so released bytes occupy absolute positions
``0..released-1`` with no gaps.

Canonical hit order (this is a contract, relied on by pagination):

    (end_offset ASC, start_offset ASC, pattern_id ASC)

At one ending position that is longest-match-first (a pattern ending at the
deepest node) followed by output-link suffixes; ties (same bytes) cannot occur
because duplicate normalized patterns are rejected. :meth:`Automaton.feed`
already emits in exactly this order, so stream order == stored order.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .automaton import Automaton, Hit
from .spec import StreamingTextDecoder, TextSpec


@dataclass(frozen=True)
class StreamStatus:
    version_id: str
    state_node: int
    bytes_consumed: int
    bytes_held: int
    epoch: int
    pattern_count: int
    node_count: int


class StreamingMatcher:
    def __init__(self, version_id: str, automaton: Automaton, spec: TextSpec):
        self._version_id = version_id
        self._automaton = automaton
        self._spec = spec
        self._decoder = StreamingTextDecoder(spec)
        self._node = automaton.root()
        # Total raw payload bytes *accepted* (held multibyte suffix included).
        self._bytes_accepted = 0
        # Bytes actually released to (and fed through) the automaton.
        self._bytes_released = 0
        # Bumped on every explicit reset. Page cursors embed the epoch they
        # were minted in, so a reset (including a version switch) invalidates
        # old cursors with a typed stale_cursor error instead of mixing state.
        self._epoch = 1

    # ---- properties ---------------------------------------------------------

    @property
    def version_id(self) -> str:
        return self._version_id

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def automaton(self) -> Automaton:
        return self._automaton

    def status(self) -> StreamStatus:
        return StreamStatus(
            version_id=self._version_id,
            state_node=self._node,
            bytes_consumed=self._bytes_accepted,
            bytes_held=self._bytes_accepted - self._bytes_released,
            epoch=self._epoch,
            pattern_count=self._automaton.pattern_count,
            node_count=self._automaton.node_count,
        )

    # ---- state transitions --------------------------------------------------

    def push(self, raw_chunk: bytes) -> List[Hit]:
        """Validate/normalize one chunk and advance the automaton.

        Returns absolute hits in canonical order. Raises domain errors
        (invalid_encoding) *before* mutating any state.
        """
        # decoder.feed raises before touching carry on a provably invalid byte.
        buf = self._decoder.feed(raw_chunk)
        # Released bytes occupy raw positions [0, total_released): no gaps
        # exist because held bytes are always a suffix.
        node, hits = self._automaton.feed(
            self._node, buf, self._bytes_released
        )
        self._node = node
        self._bytes_released += len(buf)
        self._bytes_accepted = self._bytes_released + self._decoder.held_back
        return hits

    def finish(self) -> None:
        """Declare the stream complete; reject a dangling multibyte tail."""
        self._decoder.finish()

    def restore(
        self, *, node: int, bytes_consumed: int, epoch: int
    ) -> None:
        """Restore durable streaming state (e.g. after process restart).

        Only valid against the *same* compiled automaton the node ids were
        produced by; the caller (scan service) guarantees that by loading the
        scan's pinned version first. Accepted and released bytes are equal on
        restore; a restart that landed inside an unterminated multibyte tail
        therefore treats the next chunk under a fresh (empty) carry — text
        streams should finish() before restart.
        """
        if not 0 <= node < self._automaton.node_count:
            raise ValueError(f"node {node} out of range for this automaton")
        if bytes_consumed < 0 or epoch < 1:
            raise ValueError("bytes_consumed and epoch must be non-negative/>=1")
        self._node = node
        self._bytes_accepted = bytes_consumed
        self._bytes_released = bytes_consumed
        self._epoch = epoch

    def reset(self, version_id: str, automaton: Automaton, spec: TextSpec) -> None:
        """Explicit boundary: replace automaton + spec and restart at root.

        State from before the boundary is discarded and the epoch advances.
        ``version_id`` may equal the current one (a plain rewind) — either way
        a fresh automaton instance is bound.
        """
        self._version_id = version_id
        self._automaton = automaton
        self._spec = spec
        self._decoder = StreamingTextDecoder(spec)
        self._node = automaton.root()
        self._bytes_accepted = 0
        self._bytes_released = 0
        self._epoch += 1

    def pattern_label(self, pattern_id: int) -> bytes:
        return self._automaton.patterns[pattern_id]
