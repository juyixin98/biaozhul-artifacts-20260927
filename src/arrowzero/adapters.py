"""Format adaptation layer: safe import and export of column buffers.

Three inbound formats are supported, each with an explicit validation pass
through the independent ``checks`` module before a view is handed out:

* ``pylist`` – JSON-friendly Python lists (always a fresh copy)
* ``ipc_stream`` – Arrow IPC streaming payload (zero-copy: buffers stay inside
  the payload allocation, which is pinned by the view)
* ``raw_buffers`` – explicit base64 buffers plus logical geometry (for fault
  injection tests such as decreasing offsets)
"""

from __future__ import annotations

import base64
import hashlib

import pyarrow as pa

from arrowzero.kernel import STRING_TYPE, TYPE_ALIASES, is_supported
from arrowzero.kernel.buffers import Span, span_of
from arrowzero.kernel.checks import ValidationError, ensure_valid
from arrowzero.kernel.view import ColumnView


class FormatError(ValueError):
    """Raised when a payload cannot be decoded at the format layer."""


def _decode_b64(raw: str | bytes, field: str) -> bytes:
    try:
        return base64.b64decode(raw, validate=True)
    except Exception as exc:  # binascii.Error / ValueError
        raise FormatError(f"{field} is not valid base64: {exc}") from exc


def _buffer_or_none(raw) -> pa.Buffer | None:
    if raw is None:
        return None
    return pa.py_buffer(raw)


def parse_type(type_name: str) -> pa.DataType:
    name = TYPE_ALIASES.get(type_name, type_name)
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
            raise FormatError(f"unknown/unsupported Arrow type {type_name!r}") from None
    if not is_supported(t):
        raise FormatError(
            f"type {type_name!r} is not supported (fixed-width primitives and utf8 only)"
        )
    return t

# --------------------------------------------------------------------------
# pylist
# --------------------------------------------------------------------------

def import_pylist(values: list, type_name: str) -> tuple[ColumnView, dict]:
    t = parse_type(type_name)
    try:
        array = pa.array(values, type=t)
    except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError) as exc:
        raise FormatError(f"cannot build {t!s} array from pylist: {exc}") from exc
    # Validate what we produced (defense in depth; also proves the validator
    # accepts canonical buffers).
    ensure_valid(t, len(array), array.buffers(), claimed_null_count=array.null_count)
    view = ColumnView.from_array(array, origin="pylist")
    evidence = {
        "format": "pylist",
        "zero_copy": False,
        "copied_bytes": _array_payload_bytes(array),
        "buffer_fingerprints": fingerprint_buffers(view),
    }
    return view, evidence


def _array_payload_bytes(array: pa.Array) -> int:
    return sum(b.size for b in array.buffers() if b is not None)


# --------------------------------------------------------------------------
# IPC stream
# --------------------------------------------------------------------------

def import_ipc_stream(payload: bytes) -> tuple[ColumnView, dict]:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise FormatError("ipc_stream payload must be bytes")
    payload_buf = pa.py_buffer(bytes(payload))
    payload_span = span_of(payload_buf)
    assert payload_span is not None
    try:
        reader = pa.ipc.open_stream(payload_buf)
        batch = reader.read_next_batch()
    except (pa.ArrowInvalid, OSError, EOFError) as exc:
        raise FormatError(f"invalid Arrow IPC stream: {exc}") from exc
    if batch.num_columns != 1:
        raise FormatError(
            f"IPC stream must contain exactly one column, found {batch.num_columns}"
        )
    array = batch.column(0)
    if not is_supported(array.type):
        raise FormatError(f"IPC column type {array.type!s} is not supported")
    ensure_valid(
        array.type,
        len(array),
        array.buffers(),
        logical_offset=array.offset,
        claimed_null_count=array.null_count,
    )
    # Pin the payload itself via _owners: IPC buffers live *inside* the payload
    # allocation, so dropping the payload would otherwise dangle the view.
    view = ColumnView.from_array(array, origin="ipc_stream")
    view._owners = (payload_buf,)
    spans = [span_of(b) for b in array.buffers() if b is not None]
    zero_copy = all(_inside(s, payload_span) for s in spans)
    copied = 0 if zero_copy else sum(s.size for s in spans)
    evidence = {
        "format": "ipc_stream",
        "zero_copy": zero_copy,
        "copied_bytes": copied,
        "payload_size": payload_span.size,
        "buffers_inside_payload": [
            {
                "address": s.address,
                "size": s.size,
                "payload_relative_offset": s.address - payload_span.address,
            }
            for s in spans
        ],
        "buffer_fingerprints": fingerprint_buffers(view),
    }
    return view, evidence


def _inside(inner: Span, outer: Span) -> bool:
    return outer.address <= inner.address and inner.end <= outer.end


def export_ipc_stream(view: ColumnView) -> bytes:
    array = view.to_arrow()
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, pa.schema([("", view.type)])) as writer:
        writer.write_batch(
            pa.record_batch([array], schema=pa.schema([("", view.type)]))
        )
    return sink.getvalue().to_pybytes()


# --------------------------------------------------------------------------
# raw buffers (fault-injection capable)
# --------------------------------------------------------------------------

def import_raw_buffers(descriptor: dict) -> tuple[ColumnView, dict]:
    try:
        type_name = descriptor["type"]
        length = int(descriptor["length"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FormatError(f"raw descriptor needs string 'type' and int 'length': {exc}") from exc
    offset = int(descriptor.get("offset", 0))
    t = parse_type(type_name)
    expected = 3 if t == STRING_TYPE else 2
    encoded = descriptor.get("buffers")
    if not isinstance(encoded, list) or len(encoded) != expected:
        raise FormatError(
            f"raw descriptor buffers must be a list of {expected} entries "
            f"(null for absent validity)"
        )
    decoded: list[bytes | None] = []
    for i, entry in enumerate(encoded):
        if entry is None:
            decoded.append(None)
        elif isinstance(entry, str):
            decoded.append(_decode_b64(entry, f"buffers[{i}]"))
        else:
            raise FormatError(f"buffers[{i}] must be base64 string or null")

    violations = _raw_violations(t, length, offset, decoded, descriptor)
    if violations:
        raise ValidationError(violations)

    pa_buffers = [_buffer_or_none(b) for b in decoded]
    null_count = None
    array = pa.Array.from_buffers(t, length, pa_buffers, offset=offset)
    try:
        array.validate(full=True)
    except pa.ArrowInvalid as exc:
        # Our validator and Arrow agree structurally; still surface Arrow's
        # own verdict rather than trusting only ourselves.
        raise FormatError(f"Arrow rejected the buffers even after local validation: {exc}") from exc
    null_count = array.null_count
    view = ColumnView(
        type=t,
        length=length,
        buffers=tuple(pa_buffers),
        offset=offset,
        null_count=null_count,
        origin="raw_buffers",
        _array=array,
    )
    evidence = {
        "format": "raw_buffers",
        "zero_copy": True,
        "copied_bytes": 0,
        "buffer_fingerprints": fingerprint_buffers(view),
    }
    return view, evidence


def _raw_violations(t, length, offset, decoded, descriptor):
    from arrowzero.kernel.checks import validate_buffers

    return validate_buffers(
        t,
        length,
        decoded,
        logical_offset=offset,
        claimed_null_count=None,
        check_utf8=bool(descriptor.get("check_utf8", True)),
    )


def describe_raw_violations(descriptor: dict) -> list[dict]:
    """Validate a raw descriptor without importing; used by /validate."""
    try:
        type_name = descriptor["type"]
        length = int(descriptor["length"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FormatError(f"raw descriptor needs string 'type' and int 'length': {exc}") from exc
    offset = int(descriptor.get("offset", 0))
    t = parse_type(type_name)
    expected = 3 if t == STRING_TYPE else 2
    encoded = descriptor.get("buffers")
    if not isinstance(encoded, list) or len(encoded) != expected:
        raise FormatError(
            f"raw descriptor buffers must be a list of {expected} entries "
            f"(null for absent validity)"
        )
    decoded: list[bytes | None] = []
    for i, entry in enumerate(encoded):
        decoded.append(None if entry is None else _decode_b64(entry, f"buffers[{i}]"))
    from arrowzero.kernel.checks import validate_buffers

    return [
        v.to_dict()
        for v in validate_buffers(
            t,
            length,
            decoded,
            logical_offset=offset,
            check_utf8=bool(descriptor.get("check_utf8", True)),
        )
    ]


# --------------------------------------------------------------------------
# fingerprints
# --------------------------------------------------------------------------

def fingerprint_buffers(view: ColumnView) -> list[dict]:
    names = ["validity", "offsets", "data"] if view.type == STRING_TYPE else ["validity", "data"]
    out = []
    for name, buf in zip(names, view.buffers):
        if buf is None:
            out.append({"name": name, "present": False, "sha256_16": None, "size": 0})
        else:
            digest = hashlib.sha256(bytes(buf)).hexdigest()[:16]
            out.append(
                {"name": name, "present": True, "sha256_16": digest, "size": buf.size}
            )
    return out
