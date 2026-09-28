"""Strict, bounds-checked ABI decoder.

Security properties enforced here:

* **Relative offsets.** A dynamic offset inside a container is interpreted
  relative to the start of *that container's* head, never as an absolute file
  position. Nested containers get their own ``[base, end)`` region.
* **No out-of-bounds reads.** Every slice is range-checked against both the
  container region and the whole blob before it is taken.
* **No overlap / no gaps.** Dynamic bodies must be packed contiguously and in
  head order; a pointer into a head, backwards/duplicate pointer, or a gap is
  rejected (``OffsetOutOfBounds`` / ``OffsetOverlap`` /
  ``NonCanonicalEncoding``). A global interval ledger additionally catches
  overlapping byte claims across nesting levels.
* **Bounded allocation.** A declared length/count is checked against an 8 MiB
  ceiling (with checked arithmetic) *before* any bytes/list is materialised, so
  a hostile ``0xFFFF...`` length cannot trigger a giant allocation.
* **Canonical padding.** Integer words must be zero/sign extended; right/left
  padding words must be zero; booleans must be 0/1. Each leaf is re-encoded and
  compared to the wire word.

Decoding a container is a two-pass process: a first *measure* pass reads head
pointers and length words recursively to find each dynamic body's exact extent
(without allocating payloads), and a second *value* pass decodes inside those
tight, correctly based regions. Measuring lets us distinguish a real overlap
from the benign fact that sibling bodies simply begin one after another.
"""
from __future__ import annotations

from typing import Any, List, Sequence, Tuple

from . import encoder as _enc
from .errors import (
    LengthTooLarge,
    NonCanonicalEncoding,
    NonCanonicalPadding,
    OffsetOutOfBounds,
    OffsetOverlap,
    TrailingBytes,
    ValueOutOfRange,
)
from .types import (
    WORD,
    AddressType,
    BoolType,
    BytesType,
    DynamicArrayType,
    FixedArrayType,
    FixedBytesType,
    IntType,
    StringType,
    TupleType,
    TypeHeader,
    UintType,
    parse_type,
)

DEFAULT_MAX_ALLOC = 8 * 1024 * 1024


def _head_words(header: TypeHeader) -> int:
    """Words a value occupies in a parent head: 1 for any dynamic value."""
    return 1 if header.is_dynamic else header.static_size_words()


class _Bounds:
    """Ledger of already-claimed absolute byte ranges (defence in depth)."""

    __slots__ = ("ranges",)

    def __init__(self) -> None:
        self.ranges: List[Tuple[int, int]] = []

    def claim(self, start: int, end: int) -> None:
        if start > end:
            raise OffsetOutOfBounds(f"inverted range [{start},{end})")
        for a, b in self.ranges:
            if start < b and a < end:
                raise OffsetOverlap(f"range [{start},{end}) overlaps [{a},{b})")
        self.ranges.append((start, end))


class _Region:
    """A half-open ``[base, end)`` window of the blob for ONE container."""

    __slots__ = ("blob", "base", "end", "bounds", "limit")

    def __init__(self, blob: bytes, base: int, end: int, bounds: _Bounds, limit: int):
        if base < 0 or end < base or end > len(blob):
            raise OffsetOutOfBounds(f"region [{base},{end}) outside blob of {len(blob)}")
        self.blob = blob
        self.base = base
        self.end = end
        self.bounds = bounds
        self.limit = limit

    def sub(self, base: int, end: int) -> "_Region":
        return _Region(self.blob, base, end, self.bounds, self.limit)

    def word(self, abs_pos: int) -> bytes:
        if abs_pos < 0 or abs_pos + WORD > len(self.blob):
            raise OffsetOutOfBounds(f"word at {abs_pos} outside blob")
        return self.blob[abs_pos:abs_pos + WORD]

    def uint(self, abs_pos: int) -> int:
        return int.from_bytes(self.word(abs_pos), "big", signed=False)

    def slice(self, start: int, end: int) -> bytes:
        if start < 0 or end < start or end > len(self.blob):
            raise OffsetOutOfBounds(f"slice [{start},{end}) outside blob")
        return self.blob[start:end]


# --------------------------------------------------------------------------- #
# Leaves
# --------------------------------------------------------------------------- #
def _decode_leaf(header: TypeHeader, word: bytes) -> Any:
    """Decode one 32-byte static word, rejecting non-canonical padding."""
    if isinstance(header, UintType):
        value = int.from_bytes(word, "big", signed=False)
        if value >= 1 << header.bits:
            raise ValueOutOfRange(f"value exceeds uint{header.bits}")
        if word != _enc._encode_uint(value, header.bits, signed=False):
            raise NonCanonicalPadding("non-canonical uint padding")
        return value
    if isinstance(header, IntType):
        unsigned = int.from_bytes(word, "big", signed=False)
        value = unsigned - (1 << 256) if unsigned >> 255 else unsigned
        lo, hi = -(1 << (header.bits - 1)), (1 << (header.bits - 1)) - 1
        if value < lo or value > hi:
            raise ValueOutOfRange(f"value exceeds int{header.bits}")
        if word != _enc._encode_uint(value, header.bits, signed=True):
            raise NonCanonicalPadding("non-canonical sign extension")
        return value
    if isinstance(header, BoolType):
        value = int.from_bytes(word, "big", signed=False)
        if value not in (0, 1):
            raise NonCanonicalPadding(f"bool word must be 0/1, got {value}")
        return bool(value)
    if isinstance(header, AddressType):
        value = int.from_bytes(word, "big", signed=False)
        if value >= 1 << 160:
            raise ValueOutOfRange("address exceeds 160 bits")
        if word != _enc._encode_address(value):
            raise NonCanonicalPadding("non-canonical address padding")
        return value
    if isinstance(header, FixedBytesType):
        if word[header.length:] != b"\x00" * (WORD - header.length):
            raise NonCanonicalPadding(f"non-canonical bytes{header.length} right padding")
        return bytes(word[:header.length])
    raise NonCanonicalPadding(f"not a static word type: {header.canonical()}")


def _decode_static(header: TypeHeader, reg: _Region, pos: int) -> Tuple[Any, int]:
    """Decode a fully-static value at absolute ``pos``; return (value, end)."""
    if header.static_size_words() == 1:
        return _decode_leaf(header, reg.word(pos)), pos + WORD
    if isinstance(header, FixedArrayType):
        vals: List[Any] = []
        cur = pos
        for _ in range(header.length):
            v, cur = _decode_static(header.element, reg, cur)
            vals.append(v)
        return tuple(vals), cur
    if isinstance(header, TupleType):
        vals = []
        cur = pos
        for child in header.components:
            if child.is_dynamic:
                raise NonCanonicalEncoding("static tuple cannot contain dynamic child")
            v, cur = _decode_static(child, reg, cur)
            vals.append(v)
        return tuple(vals), cur
    raise NonCanonicalEncoding(f"unexpected multi-word static type {header.canonical()}")


# --------------------------------------------------------------------------- #
# Measuring dynamic body extents (no payload allocation)
# --------------------------------------------------------------------------- #
def _measure(header: TypeHeader, reg: _Region, base: int) -> int:
    """Return absolute end of a dynamic body of ``header`` starting at ``base``.

    Only reads pointer/length words (which are bounded integers) and walks
    structural metadata; payload bytes are not materialised here.
    """
    if isinstance(header, (BytesType, StringType)):
        if base + WORD > reg.end:
            raise OffsetOutOfBounds("missing length word")
        length = reg.uint(base)
        if length > reg.limit:
            raise LengthTooLarge(f"declared length {length} exceeds limit {reg.limit}")
        data_start = base + WORD
        padded = ((length + WORD - 1) // WORD) * WORD if length else 0
        end = data_start + padded
        if data_start > reg.end or length > reg.end - data_start or end > reg.end:
            raise OffsetOutOfBounds("bytes body runs past its region")
        return end

    if isinstance(header, DynamicArrayType):
        if base + WORD > reg.end:
            raise OffsetOutOfBounds("missing array length word")
        count = reg.uint(base)
        elem_words = _head_words(header.element)
        if elem_words and count > (reg.limit // WORD) // elem_words:
            raise LengthTooLarge(f"array count {count} exceeds allocation limit")
        if count * elem_words * WORD > reg.limit:
            raise LengthTooLarge("array head exceeds allocation limit")
        elems_base = base + WORD
        if elems_base + count * elem_words * WORD > reg.end:
            raise OffsetOutOfBounds("array head runs past region")
        if header.element.is_dynamic:
            # Element pointers are relative to the array start INCLUDING the
            # length word, so the head region logically begins one word before
            # elems_base. Measure with that one-word prefix.
            return _measure_children([header.element] * count, reg, base, prefix_words=1)
        return elems_base + count * elem_words * WORD

    if isinstance(header, FixedArrayType):
        return _measure_children([header.element] * header.length, reg, base)

    if isinstance(header, TupleType):
        return _measure_children(header.components, reg, base)

    raise OffsetOutOfBounds(f"not a dynamic body: {header.canonical()}")


def _measure_children(
    children: List[TypeHeader], reg: _Region, base: int, prefix_words: int = 0
) -> int:
    head_size = (sum(_head_words(c) for c in children) + prefix_words) * WORD
    head_end = base + head_size
    if head_end > reg.end:
        raise OffsetOutOfBounds("container head runs past region")

    # Gather dynamic body starts (pointers relative to ``base``, which for a
    # dynamic array includes its leading length word when prefix_words=1).
    cursor = base + prefix_words * WORD
    dyn: List[Tuple[int, int]] = []  # (child index, absolute start)
    for idx, child in enumerate(children):
        nwords = _head_words(child)
        if child.is_dynamic:
            off = reg.uint(cursor)
            if off % WORD != 0:
                raise NonCanonicalEncoding(f"unaligned offset {off} at child {idx}")
            if off < head_size:
                raise OffsetOutOfBounds(f"offset {off} points into head (<{head_size})")
            child_abs = base + off
            if child_abs > reg.end:
                raise OffsetOutOfBounds(f"offset {off} past region")
            dyn.append((idx, child_abs))
        cursor += nwords * WORD

    # Validate pointers in head order.
    expected = head_end
    prev: int | None = None
    ends: List[int] = []
    for idx, child_abs in dyn:
        # Absolute bounds first (pointer past the blob) -> OOB.
        if child_abs >= reg.end:
            raise OffsetOutOfBounds(f"child {idx} offset points at/past region end")
        if prev is not None and child_abs <= prev:
            raise OffsetOverlap(f"child {idx} pointer does not advance")
        if child_abs < expected:
            raise OffsetOverlap(f"child {idx} body overlaps preceding bytes")
        if child_abs > expected:
            raise NonCanonicalEncoding(f"gap before child {idx} body")
        end = _measure(children[idx], reg, child_abs)
        if end > reg.end:
            raise OffsetOutOfBounds(f"child {idx} body runs past region")
        ends.append(end)
        expected = end
        prev = child_abs
    return expected


# --------------------------------------------------------------------------- #
# Value decoding inside tight regions
# --------------------------------------------------------------------------- #
def _read_bytes_like(header: TypeHeader, reg: _Region) -> Any:
    base = reg.base
    length = reg.uint(base)
    if length > reg.limit:
        raise LengthTooLarge(f"declared length {length} exceeds limit {reg.limit}")
    data_start = base + WORD
    if data_start > reg.end or length > reg.end - data_start:
        raise OffsetOutOfBounds("bytes length exceeds region")
    data_end = data_start + length
    padded = ((length + WORD - 1) // WORD) * WORD if length else 0
    pad_end = data_start + padded
    if pad_end > reg.end:
        raise OffsetOutOfBounds("padded body past region")

    reg.bounds.claim(base, data_start)
    reg.bounds.claim(data_start, data_end)
    reg.bounds.claim(data_end, pad_end)
    if reg.slice(data_end, pad_end) != b"\x00" * (pad_end - data_end):
        raise NonCanonicalPadding("non-zero padding after bytes/string")
    if pad_end != reg.end:
        raise NonCanonicalEncoding("bytes body leaves a gap or overruns region")

    data = reg.slice(data_start, data_end)
    if isinstance(header, StringType):
        try:
            return bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NonCanonicalPadding("string is not valid UTF-8") from exc
    return bytes(data)


def _read_container_children(
    children: List[TypeHeader], reg: _Region, prefix_words: int = 0
) -> List[Any]:
    n = len(children)
    head_size = (sum(_head_words(c) for c in children) + prefix_words) * WORD
    head_end = reg.base + head_size
    if head_end > reg.end:
        raise OffsetOutOfBounds("container head past region")
    # When prefixed (dynamic array), the leading length word is already claimed
    # by the caller; claim only the element-head words that follow it.
    claim_start = reg.base + prefix_words * WORD
    if head_end > claim_start:
        reg.bounds.claim(claim_start, head_end)

    values: List[Any] = [None] * n
    starts: List[int | None] = [None] * n
    cursor = reg.base + prefix_words * WORD
    for idx, child in enumerate(children):
        nwords = _head_words(child)
        if child.is_dynamic:
            off = reg.uint(cursor)
            starts[idx] = reg.base + off
        else:
            values[idx], _ = _decode_static(child, reg, cursor)
        cursor += nwords * WORD

    # Contiguity / ordering / overlap check using measured extents.
    expected = head_end
    prev: int | None = None
    for idx in range(n):
        child = children[idx]
        if not child.is_dynamic:
            continue
        child_abs = starts[idx]
        if child_abs >= reg.end:
            raise OffsetOutOfBounds(f"child {idx} offset points at/past region end")
        if prev is not None and child_abs <= prev:
            raise OffsetOverlap(f"child {idx} pointer does not advance")
        if child_abs < expected:
            raise OffsetOverlap(f"child {idx} overlaps preceding bytes")
        if child_abs > expected:
            raise NonCanonicalEncoding(f"gap before child {idx}")
        end = _measure(child, reg, child_abs)
        sub = reg.sub(child_abs, end)
        values[idx] = _read_dynamic(child, sub)
        expected = end
        prev = child_abs

    if expected != reg.end:
        raise NonCanonicalEncoding("container does not consume its full region")
    return values


def _read_dynamic(header: TypeHeader, reg: _Region) -> Any:
    if isinstance(header, (BytesType, StringType)):
        return _read_bytes_like(header, reg)
    if isinstance(header, DynamicArrayType):
        base = reg.base
        count = reg.uint(base)
        elem_words = _head_words(header.element)
        if elem_words and count > (reg.limit // WORD) // elem_words:
            raise LengthTooLarge(f"array count {count} exceeds allocation limit")
        if count * elem_words * WORD > reg.limit:
            raise LengthTooLarge("array head exceeds allocation limit")
        elems_base = base + WORD
        if elems_base + count * elem_words * WORD > reg.end:
            raise OffsetOutOfBounds("array head past region")
        reg.bounds.claim(base, elems_base)
        if header.element.is_dynamic:
            # Element pointers count the length word (prefix_words=1); keep the
            # region based at ``base`` so relative offsets resolve correctly.
            return tuple(
                _read_container_children([header.element] * count, reg, prefix_words=1)
            )
        sub = reg.sub(elems_base, reg.end)
        return tuple(_read_container_children([header.element] * count, sub))
    if isinstance(header, TupleType):
        return tuple(_read_container_children(header.components, reg))
    if isinstance(header, FixedArrayType):
        return tuple(_read_container_children([header.element] * header.length, reg))
    raise OffsetOutOfBounds(f"not a dynamic body: {header.canonical()}")


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def _root_region(blob: bytes, limit: int) -> _Region:
    return _Region(blob, 0, len(blob), _Bounds(), limit)


def decode_value(header: TypeHeader, blob: bytes, max_alloc: int = DEFAULT_MAX_ALLOC) -> Any:
    """Decode a single self-contained value."""
    reg = _root_region(blob, max_alloc)
    if header.is_dynamic:
        end = _measure(header, reg, 0)
        if end != len(blob):
            raise TrailingBytes("dynamic value does not fill blob")
        return _read_dynamic(header, reg.sub(0, end))
    need = header.static_size_words() * WORD
    if need != len(blob):
        if len(blob) > need:
            raise TrailingBytes(f"{len(blob) - need} trailing byte(s) for {header.canonical()}")
        raise OffsetOutOfBounds(f"{header.canonical()} needs {need} bytes, got {len(blob)}")
    value, end = _decode_static(header, reg, 0)
    if end != len(blob):
        raise TrailingBytes("static value does not fill blob")
    return value


def decode(
    types: Sequence[Any],
    blob: bytes,
    max_alloc: int = DEFAULT_MAX_ALLOC,
) -> Tuple[Any, ...]:
    """Decode a top-level argument tuple (head/tail sequence)."""
    headers = [t if isinstance(t, TypeHeader) else parse_type(t) for t in types]
    if not headers:
        if blob:
            raise TrailingBytes(f"{len(blob)} byte(s) for empty tuple")
        return ()
    reg = _root_region(blob, max_alloc)
    # Measure first so a hostile top-level length/offset is classified before
    # any payload is read.
    total_end = _measure_children(headers, reg, 0)
    if total_end != len(blob):
        raise TrailingBytes("top-level blob not exactly consumed")
    return tuple(_read_container_children(headers, reg))
