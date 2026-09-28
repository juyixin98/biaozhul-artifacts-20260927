"""ColumnView: an owned zero-copy view over Arrow buffers.

Invariants enforced here:

* A view holds **strong references** to every backing ``pa.Buffer`` (and to the
  source ``pa.Array``), so releasing the object the caller used to import the
  buffers can never leave the view pointing at freed memory.
* Slicing never copies: it returns another view sharing the same buffer
  addresses with a different logical ``offset`` / ``length``.
* Element access and NULL tests are computed relative to the logical offset,
  so a view produced with ``offset != 0`` stays index-correct, including for
  bitmaps (the bit is read at physical index ``offset + i``).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from app.core import bitmath, types as tt
from app.errors import ErrorCategory, LayoutError


@dataclass
class BufferIdentity:
    """Address/size snapshot of a physical buffer (used in logs and copy tests)."""

    name: str
    address: int
    size: int

    def to_dict(self) -> dict:
        return {"name": self.name, "address": hex(self.address), "size": self.size}


@dataclass
class ColumnView:
    array: pa.Array
    type_name: str
    length: int
    offset: int
    # Strong references keeping the physical memory alive.
    _owners: list[Any] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------ build
    @classmethod
    def from_array(
        cls,
        array: pa.Array,
        *,
        owners: list[Any] | None = None,
        offset: int | None = None,
        length: int | None = None,
    ) -> "ColumnView":
        # Honor an array-level offset the producer may already carry (e.g. an
        # IPC chunk): buffers() then returns the *parent* buffers, so the
        # physical index is array.offset + i — exactly what our field must hold.
        array_offset = array.offset
        array_length = len(array)
        offset = array_offset if offset is None else offset
        length = array_length if length is None else length
        if offset < 0 or length < 0 or offset + length > array_offset + array_length:
            raise LayoutError(
                ErrorCategory.SLICE_OUT_OF_RANGE,
                f"view window offset={offset} length={length} exceeds array "
                f"span offset={array_offset} length={array_length}",
            )
        owners = list(owners or [])
        # Always retain the array itself, which transitively retains its
        # buffers inside Arrow's C++ runtime.
        owners.append(array)
        for buf in array.buffers():
            if buf is not None:
                owners.append(buf)
        return cls(array=array, type_name=str(array.type), length=length,
                   offset=offset, _owners=owners)

    # ------------------------------------------------------------- properties
    @property
    def validity_buffer(self) -> pa.Buffer | None:
        return self.array.buffers()[0]

    @property
    def offsets_buffer(self) -> pa.Buffer | None:
        return self.array.buffers()[1] if tt.is_string(self.type_name) else None

    @property
    def data_buffer(self) -> pa.Buffer:
        return self.array.buffers()[2 if tt.is_string(self.type_name) else 1]

    def buffer_identities(self) -> list[BufferIdentity]:
        out: list[BufferIdentity] = []
        vb = self.validity_buffer
        if vb is not None:
            out.append(BufferIdentity("validity", vb.address, vb.size))
        if self.offsets_buffer is not None:
            out.append(BufferIdentity("offsets", self.offsets_buffer.address, self.offsets_buffer.size))
        out.append(BufferIdentity("data", self.data_buffer.address, self.data_buffer.size))
        return out

    def shares_memory_with(self, other: "ColumnView") -> dict[str, bool]:
        """Per-buffer pointer equality: the zero-copy proof used by the tests."""
        mine = {b.name: b for b in self.buffer_identities()}
        theirs = {b.name: b for b in other.buffer_identities()}
        return {name: mine[name].address == theirs[name].address
                for name in mine.keys() & theirs.keys()}

    # -------------------------------------------------------------- accessors
    def is_null(self, i: int) -> bool:
        self._check_logical(i)
        vb = self.validity_buffer
        if vb is None:
            return False
        # Bitmap is shared with the parent; bit position must add the offset.
        return bitmath.bit_get(vb, self.offset + i) == 0

    def logical_null_count(self) -> int:
        vb = self.validity_buffer
        if vb is None:
            return 0
        return self.length - bitmath.count_set_bits(vb, self.offset, self.length)

    def value(self, i: int) -> Any:
        """Return the logical element (Python value) or ``None``."""
        self._check_logical(i)
        if self.is_null(i):
            return None
        if tt.is_string(self.type_name):
            start, end = self._string_span(i)
            return bytes(self.data_buffer[start:end]).decode("utf-8")
        width = tt.byte_width(self.type_name)
        physical = (self.offset + i) * width
        return self._decode_fixed(bytes(self.data_buffer[physical:physical + width]))

    def to_pylist(self) -> list[Any]:
        return [self.value(i) for i in range(self.length)]

    def _decode_fixed(self, raw: bytes) -> Any:
        tn = self.type_name
        if tn in ("int8",):
            return struct.unpack("<b", raw)[0]
        if tn in ("uint8",):
            return raw[0]
        if tn == "int16":
            return struct.unpack("<h", raw)[0]
        if tn == "uint16":
            return struct.unpack("<H", raw)[0]
        if tn == "int32":
            return struct.unpack("<i", raw)[0]
        if tn == "uint32":
            return struct.unpack("<I", raw)[0]
        if tn == "int64":
            return struct.unpack("<q", raw)[0]
        if tn == "uint64":
            return struct.unpack("<Q", raw)[0]
        if tn == "float":
            return struct.unpack("<f", raw)[0]
        if tn == "double":
            return struct.unpack("<d", raw)[0]
        raise LayoutError(ErrorCategory.UNSUPPORTED_TYPE, f"cannot decode {tn}")

    def _string_span(self, i: int) -> tuple[int, int]:
        """Physical [start, end) byte span for the i-th logical string."""
        ob = self.offsets_buffer
        assert ob is not None
        base = (self.offset + i) * 4
        start = struct.unpack("<i", bytes(ob[base:base + 4]))[0]
        end = struct.unpack("<i", bytes(ob[base + 4:base + 8]))[0]
        return start, end

    def _check_logical(self, i: int) -> None:
        if not 0 <= i < self.length:
            raise LayoutError(
                ErrorCategory.SLICE_OUT_OF_RANGE,
                f"logical index {i} out of range for view length={self.length}",
            )

    # ------------------------------------------------------------ operations
    def slice(self, offset: int, length: int) -> "ColumnView":
        """Zero-copy slice: same buffers, shifted logical window."""
        if offset < 0 or length < 0 or offset + length > self.length:
            raise LayoutError(
                ErrorCategory.SLICE_OUT_OF_RANGE,
                f"slice(offset={offset}, length={length}) out of range; view length={self.length}",
            )
        new_offset = self.offset + offset
        return ColumnView(
            array=self.array,
            type_name=self.type_name,
            length=length,
            offset=new_offset,
            _owners=list(self._owners),
        )

    def materialize(self) -> pa.Array:
        """Physical Arrow array covering exactly this logical window (a copy)."""
        return self.array.slice(self.offset, self.length)
