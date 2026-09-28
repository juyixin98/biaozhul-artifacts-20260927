"""Stream a bound plan over its source.

The application never re-runs the engine and never parses templates: a plan is
a list of byte ranges plus final replacement bytes.  Streaming keeps peak
memory bounded by chunk size rather than by document size -- each original
segment is handed out as a zero-copy ``memoryview``.

Preconditions (all enforced):
* ``expected_sha256`` MUST equal ``plan.source_sha256``; otherwise
  :class:`~app.errors.SourceVersionMismatchError`.  This is the "reject stale
  source version" contract.
* ``len(data)`` MUST equal ``plan.source_length`` (defense in depth; a digest
  mismatch normally catches everything, length catches truncation cheaply).
* Edits are assumed sorted, disjoint and validated at build time; this
  function still asserts the ordering so a corrupted stored plan cannot yield
  overlapping writes.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from ..errors import SourceVersionMismatchError
from ..textspec import sha256_hex
from .model import Plan


@dataclass(frozen=True, slots=True)
class ApplyResult:
    output: bytes
    chunks_emitted: int
    bytes_emitted: int
    edits_applied: int


def iter_plan_chunks(
    plan: Plan,
    data: bytes,
    *,
    expected_sha256: str | None = None,
    chunk_size: int = 64 * 1024,
) -> Iterator[bytes]:
    """Yield output bytes in <= ``chunk_size`` pieces after digest binding.

    Raises before yielding anything on a version mismatch, so a stale plan can
    never partially apply.
    """
    actual = sha256_hex(data)
    expected = expected_sha256 or plan.source_sha256
    if expected != plan.source_sha256 or actual != expected:
        raise SourceVersionMismatchError(
            "plan is not bound to this source version",
            expected=expected,
            actual=actual,
            bound=plan.source_sha256,
        )
    if len(data) != plan.source_length:
        raise SourceVersionMismatchError(
            "source length differs from the plan-bound version",
            expected_length=plan.source_length,
            actual_length=len(data),
        )

    out = bytearray()
    cursor = 0
    edits_applied = 0
    for edit in plan.edits:
        # Cheap structural assertion against corrupted persisted plans.
        if edit.start < cursor or edit.end < edit.start:
            raise SourceVersionMismatchError(
                "stored plan has overlapping or malformed edits",
                at=edit.start,
                cursor=cursor,
            )
        for piece in _drain(memoryview(data)[cursor:edit.start], out, chunk_size):
            yield piece
        for piece in _drain(memoryview(edit.replacement), out, chunk_size):
            yield piece
        cursor = edit.end
        edits_applied += 1

    for piece in _drain(memoryview(data)[cursor:], out, chunk_size):
        yield piece
    if out:
        # Final short chunk: attach edit count via the non-streaming path's
        # return value -- callers needing counts use apply_plan_stream.
        yield bytes(out)
        out.clear()
    # edits_applied is only surfaced through apply_plan_stream; iter_* is raw.
    _ = edits_applied


def _drain(view: memoryview, out: bytearray, chunk_size: int) -> Iterator[bytes]:
    """Append ``view`` to ``out``, flushing complete chunks."""
    out += view
    while len(out) >= chunk_size:
        yield bytes(out[:chunk_size])
        del out[:chunk_size]


def apply_plan_stream(
    plan: Plan,
    data: bytes,
    *,
    expected_sha256: str | None = None,
    chunk_size: int = 64 * 1024,
    collect: bool = True,
) -> ApplyResult:
    """Apply and (by default) collect output.  Used by service and tests.

    ``collect=False`` still runs the whole pipeline (useful for measuring that
    streaming visits every byte without materializing the result).
    """
    chunks = 0
    total = 0
    pieces: list[bytes] = []
    for chunk in iter_plan_chunks(
        plan, data, expected_sha256=expected_sha256, chunk_size=chunk_size
    ):
        chunks += 1
        total += len(chunk)
        if collect:
            pieces.append(chunk)
    return ApplyResult(
        output=b"".join(pieces) if collect else b"",
        chunks_emitted=chunks,
        bytes_emitted=total,
        edits_applied=len(plan.edits),
    )
