"""Supported Arrow type table.

Only fixed-width primitive types and the variable-length UTF-8 string type are
in scope. ``bool`` is deliberately excluded: its bit-packed data buffer needs
different length/extraction rules and would blur the independent data-buffer
check; ``large_utf8`` (64-bit offsets) is excluded so the offset contract stays
single and explicit.

Naming: the *canonical* name is PyArrow's ``str(type)`` spelling (``string``,
``double`` ...). The common aliases ``utf8`` and ``float64`` are accepted on
input so both Arrow dialects are usable in descriptors.
"""
from __future__ import annotations

import pyarrow as pa

FIXED_WIDTH: dict[str, int] = {
    "int8": 1,
    "uint8": 1,
    "int16": 2,
    "uint16": 2,
    "int32": 4,
    "uint32": 4,
    "int64": 8,
    "uint64": 8,
    "float": 4,
    "double": 8,
}
STRING_TYPE = "string"
SUPPORTED_TYPES: frozenset[str] = frozenset(FIXED_WIDTH) | {STRING_TYPE}

# Descriptor aliases -> canonical PyArrow spelling.
ALIASES: dict[str, str] = {
    "utf8": "string",
    "large_utf8": "large_string",  # mapped only to produce a clear unsupported error
    "float32": "float",
    "float64": "double",
    "halffloat": "half_float",
}

_PA_TYPE_FACTORIES = {
    "int8": pa.int8,
    "uint8": pa.uint8,
    "int16": pa.int16,
    "uint16": pa.uint16,
    "int32": pa.int32,
    "uint32": pa.uint32,
    "int64": pa.int64,
    "uint64": pa.uint64,
    "float": pa.float32,
    "double": pa.float64,
    STRING_TYPE: pa.utf8,
}


def canonical(type_name: str) -> str:
    return ALIASES.get(type_name, type_name)


def is_supported(type_name: str) -> bool:
    return canonical(type_name) in SUPPORTED_TYPES


def is_string(type_name: str) -> bool:
    return canonical(type_name) == STRING_TYPE


def byte_width(type_name: str) -> int:
    """Fixed-width byte size; raises KeyError for variable-width types."""
    return FIXED_WIDTH[canonical(type_name)]


def pa_type(type_name: str) -> pa.DataType:
    canon = canonical(type_name)
    try:
        return _PA_TYPE_FACTORIES[canon]()
    except KeyError:
        from app.errors import ErrorCategory, LayoutError

        raise LayoutError(
            ErrorCategory.UNSUPPORTED_TYPE,
            f"unsupported type {type_name!r}; supported: {sorted(SUPPORTED_TYPES)} "
            f"(aliases: utf8, float32, float64)",
        ) from None
