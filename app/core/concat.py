"""Concat execution kernel.

Rules enforced by this kernel:

* Concatenating two or more views **copies** their physical bytes into fresh,
  contiguous buffers (as Arrow concatenation must). Every copied byte is
  counted and returned so tests can assert the real copy volume.
* Inputs may be sliced views (``offset != 0``); only the logical window of each
  is copied, read relative to that offset.
* Inputs of different Arrow types are rejected with ``type_mismatch``; the
  caller must pass ``target_type`` to request an explicit cast first. A cast is
  itself a conversion and its output bytes are counted separately.
* Validity bitmaps are merged by reading logical bits (offset-aware), never by
  concatenating raw bitmap bytes (which would misalign byte boundaries).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import pyarrow as pa

from app.core import bitmath, types as tt
from app.core.columnview import ColumnView
from app.errors import ErrorCategory, LayoutError


@dataclass
class CopyReport:
    bytes_validity: int = 0
    bytes_offsets: int = 0
    bytes_data: int = 0
    bytes_cast: int = 0
    cast_target: str | None = None
    inputs: int = 0
    zero_copy: bool = False
    steps: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return self.bytes_validity + self.bytes_offsets + self.bytes_data + self.bytes_cast

    def to_dict(self) -> dict:
        return {
            "copied_bytes": {
                "validity": self.bytes_validity,
                "offsets": self.bytes_offsets,
                "data": self.bytes_data,
                "cast": self.bytes_cast,
                "total": self.total_bytes,
            },
            "cast_target": self.cast_target,
            "inputs": self.inputs,
            "zero_copy": self.zero_copy,
            "steps": self.steps,
        }


@dataclass
class ConcatResult:
    view: ColumnView
    report: CopyReport


def concat(views: list[ColumnView], target_type: str | None = None) -> ConcatResult:
    if not views:
        raise LayoutError(ErrorCategory.MALFORMED_PAYLOAD, "concat requires at least one input view")
    report = CopyReport(inputs=len(views))

    # Explicit cross-type conversion happens before concatenation.
    if target_type is not None:
        target_type = tt.canonical(target_type)
        if not tt.is_supported(target_type):
            raise LayoutError(ErrorCategory.UNSUPPORTED_TYPE, f"cast target {target_type!r} is not supported")
        report.cast_target = target_type
        views = [_cast_view(v, target_type, report) for v in views]
    else:
        distinct = {v.type_name for v in views}
        if len(distinct) > 1:
            raise LayoutError(
                ErrorCategory.TYPE_MISMATCH,
                "cannot concatenate views of different types; pass target_type to cast explicitly",
                detail={"types": sorted(distinct)},
            )

    type_name = views[0].type_name
    # A single input needs no copies: return the same owned view.
    if len(views) == 1:
        report.zero_copy = True
        report.steps.append("single input: returned existing view with no copies")
        return ConcatResult(views[0], report)

    total_length = sum(v.length for v in views)
    validity = _merge_validity(views, total_length, report)
    if tt.is_string(type_name):
        offsets, data = _concat_strings(views, report)
        buffers = [validity, offsets, data]
    else:
        data = _concat_fixed(views, type_name, report)
        buffers = [validity, data]

    pa_buffers: list[pa.Buffer | None] = []
    owners: list[object] = []
    for raw in buffers:
        if raw is None:
            pa_buffers.append(None)
        else:
            pb = pa.py_buffer(memoryview(raw))
            owners.append(raw)
            owners.append(pb)
            pa_buffers.append(pb)

    null_count = None if validity is None else total_length - bitmath.count_set_bits(validity, 0, total_length)
    array = pa.Array.from_buffers(
        tt.pa_type(type_name), total_length, pa_buffers,
        -1 if null_count is None else null_count,
    )
    view = ColumnView.from_array(array, owners=owners)
    report.steps.append(f"built contiguous {type_name} array of {total_length} elements")
    return ConcatResult(view, report)


# --------------------------------------------------------------------- cast
def _cast_view(view: ColumnView, target: str, report: CopyReport) -> ColumnView:
    if view.type_name == target:
        report.steps.append(f"segment already {target}: no cast")
        return view
    window = view.array.slice(view.offset, view.length)
    try:
        casted = window.cast(tt.pa_type(target))
    except pa.ArrowInvalid as exc:
        raise LayoutError(
            ErrorCategory.TYPE_MISMATCH,
            f"explicit cast {view.type_name} -> {target} failed: {exc}",
        ) from exc
    casted_view = ColumnView.from_array(casted, owners=[view])
    casted_length = casted_view.length
    casted_offset = casted_view.offset
    # casted may itself be a view sharing parent buffers (when ``window`` was a
    # slice); measure only the bytes the cast *produced* for the logical window.
    if tt.is_string(target):
        produced = casted_view.offsets_buffer.size if casted_view.offsets_buffer else 0
        ob = casted_view.offsets_buffer
        if ob is not None and casted_length >= 1:
            first = struct.unpack("<i", bytes(ob[casted_offset * 4:casted_offset * 4 + 4]))[0]
            after = struct.unpack(
                "<i", bytes(ob[(casted_offset + casted_length) * 4:
                               (casted_offset + casted_length) * 4 + 4]))[0]
            produced += after - first
        if casted_view.validity_buffer is not None:
            produced += bitmath.ceil_div8(casted_offset + casted_length)
    else:
        produced = casted_length * tt.byte_width(target)
        if casted_view.validity_buffer is not None:
            produced += bitmath.ceil_div8(casted_offset + casted_length)
    report.bytes_cast += produced
    report.steps.append(
        f"cast segment {view.type_name}->{target}: produced ~{produced} bytes "
        f"for {casted_length} logical elements"
    )
    return casted_view


# ----------------------------------------------------------------- validity
def _merge_validity(views: list[ColumnView], total_length: int, report: CopyReport) -> bytes | None:
    any_null = any(v.validity_buffer is not None for v in views)
    if not any_null:
        report.steps.append("validity: no segment carries a bitmap -> all valid, bitmap omitted")
        return None
    flags: list[int] = []
    for v in views:
        vb = v.validity_buffer
        for i in range(v.length):
            if vb is None:
                flags.append(1)
            else:
                # Logical bit position inside the (possibly shared) parent bitmap.
                flags.append(bitmath.bit_get(vb, v.offset + i))
    packed = bitmath.pack_validity(flags)
    report.bytes_validity = len(packed)
    report.steps.append(
        f"validity: packed {sum(1 for f in flags if not f)} NULLs over {total_length} slots "
        f"-> {len(packed)} bitmap bytes (bitwise logical merge, no raw-byte concat)"
    )
    return packed


# -------------------------------------------------------------------- fixed
def _concat_fixed(views: list[ColumnView], type_name: str, report: CopyReport) -> bytes:
    width = tt.byte_width(type_name)
    out = bytearray()
    for idx, v in enumerate(views):
        start = v.offset * width
        end = (v.offset + v.length) * width
        chunk = bytes(v.data_buffer[start:end])
        out.extend(chunk)
        report.steps.append(
            f"data[{idx}]: copied physical bytes [{start},{end}) = {len(chunk)} bytes "
            f"from data@0x{v.data_buffer.address:x}"
        )
    report.bytes_data = len(out)
    return bytes(out)


# ----------------------------------------------------------------- strings
def _concat_strings(views: list[ColumnView], report: CopyReport) -> tuple[bytes, bytes]:
    data = bytearray()
    offsets = bytearray(struct.pack("<i", 0))
    running = 0
    for idx, v in enumerate(views):
        ob = v.offsets_buffer
        assert ob is not None
        for i in range(v.length):
            if v.is_null(i):
                # NULL entries carry a zero-width span in the rebuilt array.
                offsets.extend(struct.pack("<i", running))
                continue
            start, end = v._string_span(i)
            chunk = bytes(v.data_buffer[start:end])
            data.extend(chunk)
            running += len(chunk)
            offsets.extend(struct.pack("<i", running))
        report.steps.append(
            f"strings[{idx}]: appended {v.length} logical elements, data buffer now {running} bytes"
        )
    report.bytes_offsets = len(offsets)
    report.bytes_data = len(data)
    return bytes(offsets), bytes(data)
