"""Buffer address/span helpers used for zero-copy evidence and copy accounting."""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa


@dataclass(frozen=True)
class Span:
    address: int
    size: int

    @property
    def end(self) -> int:
        return self.address + self.size

    def overlaps(self, other: "Span") -> bool:
        return self.address < other.end and other.address < self.end


def span_of(buf: pa.Buffer | None) -> Span | None:
    if buf is None:
        return None
    return Span(address=buf.address, size=buf.size)


def is_zero_copy_slice(child: pa.Buffer | None, parent: pa.Buffer | None) -> bool:
    """True iff ``child`` is a view backed by ``parent``'s allocation."""
    if child is None and parent is None:
        return True
    if child is None or parent is None:
        return False
    cs, ps = span_of(child), span_of(parent)
    assert cs is not None and ps is not None
    return ps.address <= cs.address and cs.end <= ps.end


def shared_with_any(child: pa.Buffer | None, parents: list[Span]) -> bool:
    if child is None:
        return True
    cs = span_of(child)
    assert cs is not None
    return any(ps.address <= cs.address and cs.end <= ps.end for ps in parents)
