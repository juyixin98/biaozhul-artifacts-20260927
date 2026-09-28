"""Zero-copy column views.

A ``ColumnView`` owns two distinct things:

* **Buffer ownership** – a tuple of the underlying ``pyarrow.Buffer`` objects.
  Keeping these (not the source array wrapper) pins the allocation so a view
  stays valid after the source array, batch or IPC reader is deleted.
* **Logical geometry** – Arrow ``offset``/``length`` over those buffers.

Slicing never allocates: it produces another view over the same buffer tuple.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa

from arrowzero.kernel import STRING_TYPE, primitive_spec
from arrowzero.kernel.bitmap import is_bit_set


@dataclass
class ColumnView:
    type: pa.DataType
    length: int
    buffers: tuple[pa.Buffer | None, ...]  # validity[, offsets], data; pins memory
    offset: int = 0
    null_count: int | None = None  # None = unknown, lazily counted
    origin: str = "unknown"  # provenance for logs/descriptors
    _array: pa.Array | None = field(default=None, repr=False)
    _owners: tuple = field(default=(), repr=False)  # extra pinned allocations (e.g. IPC payload)

    # ----- construction ---------------------------------------------------

    @classmethod
    def from_array(cls, array: pa.Array, *, origin: str = "array") -> "ColumnView":
        array = array.combine_chunks() if isinstance(array, pa.ChunkedArray) else array
        bufs = tuple(array.buffers())
        return cls(
            type=array.type,
            length=len(array),
            buffers=bufs,
            offset=array.offset,
            null_count=array.null_count,
            origin=origin,
            _array=array,
        )

    # ----- NULL / value semantics (independent of pyarrow accessors) ------

    def is_null(self, index: int) -> bool:
        if not 0 <= index < self.length:
            raise IndexError(f"index {index} out of range for length {self.length}")
        validity = self.buffers[0]
        if validity is None:
            return False
        return not is_bit_set(validity, self.offset + index)

    def get(self, index: int) -> Any:
        if self.is_null(index):
            return None
        if self.type == STRING_TYPE:
            return self._get_string(index)
        return self._get_primitive(index)

    def _get_primitive(self, index: int) -> Any:
        spec = primitive_spec(self.type)
        data = self.buffers[1]
        assert data is not None
        flat = np.frombuffer(data, dtype=spec.numpy_dtype)
        return flat[self.offset + index].item()

    def _get_string(self, index: int) -> str:
        offsets_buf, data_buf = self.buffers[1], self.buffers[2]
        assert offsets_buf is not None and data_buf is not None
        offsets = np.frombuffer(offsets_buf, dtype=np.int32)
        pos = self.offset + index
        start, end = int(offsets[pos]), int(offsets[pos + 1])
        return data_buf.slice(start, end - start).to_pybytes().decode("utf-8")

    def count_nulls(self) -> int:
        if self.null_count is not None:
            return self.null_count
        from arrowzero.kernel.bitmap import count_set_bits

        validity = self.buffers[0]
        if validity is None:
            self.null_count = 0
        else:
            self.null_count = self.length - count_set_bits(
                validity, self.length, self.offset
            )
        return self.null_count

    # ----- materialization (lazily cached; shares the pinned buffers) -----

    def to_arrow(self) -> pa.Array:
        if self._array is None or len(self._array) != self.length or self._array.offset != self.offset:
            self._array = pa.Array.from_buffers(
                self.type,
                self.length,
                list(self.buffers),
                offset=self.offset,
                null_count=self.count_nulls(),
            )
        return self._array

    def to_pylist(self) -> list[Any]:
        # Driven by the kernel's own NULL/index semantics, used by tests only;
        # production oracle code compares against pyarrow directly.
        return [self.get(i) for i in range(self.length)]

    # ----- the zero-copy operation ---------------------------------------

    def slice(self, offset: int, length: int | None = None) -> "ColumnView":
        if offset < 0:
            raise IndexError(f"slice offset must be >= 0, got {offset}")
        if length is None:
            length = self.length - offset
        if length < 0:
            raise IndexError(f"slice length must be >= 0, got {length}")
        if offset + length > self.length:
            raise IndexError(
                f"slice [{offset}, {offset + length}) exceeds view length {self.length}"
            )
        # Determine the slice's null count eagerly enough for from_buffers but
        # leave None unless the parent already knows — recompute via bitmap.
        if length == 0:
            slice_nulls = 0
        elif self.count_nulls() == 0:
            slice_nulls = 0
        else:
            from arrowzero.kernel.bitmap import count_set_bits

            validity = self.buffers[0]
            if validity is None:
                slice_nulls = 0
            else:
                slice_nulls = length - count_set_bits(
                    validity, length, self.offset + offset
                )
        return ColumnView(
            type=self.type,
            length=length,
            buffers=self.buffers,  # same tuple: zero copy, shared ownership
            offset=self.offset + offset,
            null_count=slice_nulls,
            origin=f"slice@{self.origin}",
            _owners=self._owners,
        )

    # ----- descriptors -----------------------------------------------------

    def buffer_spans(self) -> list[dict]:
        names = self._buffer_names()
        out = []
        for name, buf in zip(names, self.buffers):
            out.append(
                {
                    "name": name,
                    "address": None if buf is None else buf.address,
                    "size": None if buf is None else buf.size,
                }
            )
        return out

    def _buffer_names(self) -> list[str]:
        return ["validity", "offsets", "data"] if self.type == STRING_TYPE else ["validity", "data"]

    def describe(self) -> dict:
        return {
            "type": str(self.type),
            "length": self.length,
            "offset": self.offset,
            "null_count": self.count_nulls(),
            "origin": self.origin,
            "buffers": self.buffer_spans(),
        }
