"""Concatenation kernel with explicit casts and measured copy accounting.

Concatenation *materializes* a new dense array: a non-zero first offset or a
chain of separate source allocations cannot be expressed as one Arrow array
without a copy, and this module never pretends otherwise. Every newly
allocated byte is reported through a ``CopyLedger`` distinguishing fresh
allocation from buffers that merely alias a source span.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pyarrow as pa

from arrowzero.kernel import OFFSET_WIDTH, STRING_TYPE, TYPE_ALIASES, primitive_spec
from arrowzero.kernel.bitmap import build_validity
from arrowzero.kernel.buffers import Span, span_of
from arrowzero.kernel.view import ColumnView


@dataclass
class BufferCopy:
    name: str
    address: int
    size: int
    reused_source_address: int | None  # when the buffer aliases a source span
    copied: bool


@dataclass
class CopyLedger:
    """Records where each output buffer lives relative to source allocations."""

    sources: list[Span] = field(default_factory=list)
    buffers: list[BufferCopy] = field(default_factory=list)
    allocator_before: int = 0

    @classmethod
    def for_inputs(cls, views: list[ColumnView]) -> "CopyLedger":
        sources: list[Span] = []
        for v in views:
            for buf in v.buffers:
                span = span_of(buf)
                if span is not None:
                    sources.append(span)
        return cls(sources=sources, allocator_before=pa.total_allocated_bytes())

    def record(self, name: str, buf: pa.Buffer | None) -> None:
        if buf is None:
            self.buffers.append(
                BufferCopy(name, address=0, size=0, reused_source_address=None, copied=False)
            )
            return
        span = span_of(buf)
        assert span is not None
        reused = next(
            (
                s.address
                for s in self.sources
                if s.address <= span.address and span.end <= s.end
            ),
            None,
        )
        self.buffers.append(
            BufferCopy(
                name,
                address=span.address,
                size=span.size,
                reused_source_address=reused,
                copied=reused is None and span.size > 0,
            )
        )

    @property
    def copied_bytes(self) -> int:
        return sum(b.size for b in self.buffers if b.copied)

    @property
    def allocated_bytes(self) -> int:
        """Bytes of output buffers that do not live inside a source span."""
        return sum(b.size for b in self.buffers if b.reused_source_address is None)

    @property
    def allocator_delta(self) -> int:
        return pa.total_allocated_bytes() - self.allocator_before

    def as_dict(self) -> dict:
        return {
            "output_buffers": [
                {
                    "name": b.name,
                    "address": b.address or None,
                    "size": b.size,
                    "aliased_source": b.reused_source_address,
                    "copied": b.copied,
                }
                for b in self.buffers
            ],
            "copied_bytes": self.copied_bytes,
            "allocated_bytes": self.allocated_bytes,
            "allocator_delta_bytes": self.allocator_delta,
        }


class CastError(TypeError):
    """Raised when inputs differ in type and no/ an impossible cast is given."""


def _resolve_type(cast_to: pa.DataType | str) -> pa.DataType:
    if isinstance(cast_to, pa.DataType):
        t = cast_to
    else:
        name = TYPE_ALIASES.get(cast_to, cast_to)
        if name == "utf8":
            t = pa.utf8()
        elif name == "float":
            t = pa.float32()
        elif name == "double":
            t = pa.float64()
        else:
            try:
                t = getattr(pa, name)()
            except AttributeError:
                raise CastError(f"unknown/unsupported cast target {cast_to!r}") from None
    if t == STRING_TYPE:
        return t
    primitive_spec(t)  # raises TypeError if unsupported
    return t


def _target_type(views: list[ColumnView], cast_to: pa.DataType | str | None) -> pa.DataType:
    types = {v.type for v in views}
    if cast_to is not None:
        target = _resolve_type(cast_to)
        for v in views:
            if v.type == target:
                continue
            if v.type == STRING_TYPE or target == STRING_TYPE:
                raise CastError(
                    f"explicit cast {v.type!s} -> {target!s} is not supported by this kernel"
                )
        return target
    if len(types) == 1:
        return next(iter(types))
    raise CastError(
        "inputs have differing types "
        + ", ".join(sorted(str(t) for t in types))
        + "; pass cast_to= to convert explicitly (cross-type concat never auto-casts)"
    )


def concat(
    views: list[ColumnView],
    *,
    cast_to: pa.DataType | str | None = None,
) -> tuple[ColumnView, CopyLedger]:
    if not views:
        raise ValueError("concat requires at least one input view")
    target = _target_type(views, cast_to)
    ledger = CopyLedger.for_inputs(views)
    if target == STRING_TYPE:
        return _concat_strings(views, ledger)
    return _concat_primitives(views, target, ledger)


def _concat_primitives(
    views: list[ColumnView], target: pa.DataType, ledger: CopyLedger
) -> ColumnView:
    spec = primitive_spec(target)
    total = sum(v.length for v in views)
    valid_flags = [not v.is_null(i) for v in views for i in range(v.length)]
    validity_bytes = build_validity(valid_flags)
    validity_buf = pa.py_buffer(validity_bytes) if validity_bytes is not None else None

    # One fresh allocation per output buffer; wrap it writably via numpy.
    data_buf = pa.allocate_buffer(total * spec.byte_width)
    out = np.frombuffer(data_buf, dtype=spec.numpy_dtype)
    pos = 0
    for v in views:
        src_spec = primitive_spec(v.type)  # strings rejected by _target_type
        chunk = np.frombuffer(v.buffers[1], dtype=src_spec.numpy_dtype)[
            v.offset : v.offset + v.length
        ]
        nulls = np.fromiter((v.is_null(i) for i in range(v.length)), dtype=bool, count=v.length)
        # Safe cast: reject narrowing that would overflow/truncate rather than
        # silently wrapping (numpy astype alone would hide this).
        try:
            converted = chunk.astype(spec.numpy_dtype, casting="safe", copy=True)
        except TypeError as exc:
            raise CastError(
                f"unsafe cast {v.type!s} -> {target!s}: {exc}"
            ) from exc
        converted[nulls] = 0
        out[pos : pos + v.length] = converted
        pos += v.length

    ledger.record("validity", validity_buf)
    ledger.record("data", data_buf)
    array = pa.Array.from_buffers(
        target,
        total,
        [validity_buf, data_buf],
        null_count=total - sum(valid_flags),
    )
    return ColumnView.from_array(array, origin="concat"), ledger


def _concat_strings(views: list[ColumnView], ledger: CopyLedger) -> ColumnView:
    total = sum(v.length for v in views)
    valid_flags = [not v.is_null(i) for v in views for i in range(v.length)]
    validity_bytes = build_validity(valid_flags)
    validity_buf = pa.py_buffer(validity_bytes) if validity_bytes is not None else None

    # Exact output data span: each chunk contributes [local start, local end).
    chunk_windows: list[tuple[int, int]] = []
    total_data = 0
    for v in views:
        offsets = np.frombuffer(v.buffers[1], dtype=np.int32)
        lo = int(offsets[v.offset])
        hi = int(offsets[v.offset + v.length])
        chunk_windows.append((lo, hi))
        total_data += hi - lo

    offsets_buf = pa.allocate_buffer((total + 1) * OFFSET_WIDTH)
    data_buf = pa.allocate_buffer(total_data)
    out_offsets = np.frombuffer(offsets_buf, dtype=np.int32)
    out_data = memoryview(data_buf)

    running = 0
    elem = 0
    out_offsets[0] = 0
    for v, (lo, hi) in zip(views, chunk_windows):
        src_offsets = np.frombuffer(v.buffers[1], dtype=np.int32)
        src_data = v.buffers[2]
        assert src_data is not None
        for i in range(v.length):
            if not valid_flags[elem]:
                out_offsets[elem + 1] = running  # NULL: zero-width span
            else:
                start = int(src_offsets[v.offset + i])
                end = int(src_offsets[v.offset + i + 1])
                payload = src_data.slice(start, end - start)
                out_data[running : running + payload.size] = memoryview(payload)
                running += payload.size
                out_offsets[elem + 1] = running
            elem += 1

    ledger.record("validity", validity_buf)
    ledger.record("offsets", offsets_buf)
    ledger.record("data", data_buf)
    array = pa.Array.from_buffers(
        STRING_TYPE,
        total,
        [validity_buf, offsets_buf, data_buf],
        null_count=total - sum(valid_flags),
    )
    return ColumnView.from_array(array, origin="concat"), ledger
