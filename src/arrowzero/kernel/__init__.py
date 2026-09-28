"""Arrow primitive type registry shared by the kernel, adapters and checks.

Only fixed-width primitive numeric types and utf8 strings are in scope.
Booleans and other types are deliberately rejected rather than silently
handled with wrong byte widths.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass(frozen=True)
class PrimitiveSpec:
    pa_type: pa.DataType
    numpy_dtype: np.dtype
    byte_width: int


def _spec(t, np_name: str, width: int) -> PrimitiveSpec:
    return PrimitiveSpec(t, np.dtype(np_name), width)


_PRIMITIVES: dict[str, PrimitiveSpec] = {
    "int8": _spec(pa.int8(), "int8", 1),
    "int16": _spec(pa.int16(), "int16", 2),
    "int32": _spec(pa.int32(), "int32", 4),
    "int64": _spec(pa.int64(), "int64", 8),
    "uint8": _spec(pa.uint8(), "uint8", 1),
    "uint16": _spec(pa.uint16(), "uint16", 2),
    "uint32": _spec(pa.uint32(), "uint32", 4),
    "uint64": _spec(pa.uint64(), "uint64", 8),
    # str(pa.float32()) == "float", str(pa.float64()) == "double" in Arrow.
    "float": _spec(pa.float32(), "float32", 4),
    "double": _spec(pa.float64(), "float64", 8),
}

# Public aliases accepted from JSON/API alongside canonical Arrow type names.
TYPE_ALIASES = {
    "float32": "float",
    "float64": "double",
    "string": "utf8",
    "utf8": "utf8",
}

STRING_TYPE = pa.utf8()
OFFSET_WIDTH = 4  # Arrow int32 offsets for utf8


def primitive_spec(pa_type: pa.DataType) -> PrimitiveSpec:
    name = str(pa_type)
    try:
        return _PRIMITIVES[name]
    except KeyError:
        raise TypeError(f"unsupported primitive type: {name!r}") from None


def is_supported(pa_type: pa.DataType) -> bool:
    return str(pa_type) in _PRIMITIVES or pa_type == STRING_TYPE


def validity_byte_size(length: int) -> int:
    return max(1, (length + 7) // 8)
