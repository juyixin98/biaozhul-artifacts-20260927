"""PyArrow format adapter.

Responsibilities:

1. Translate the canonical DSL schema into a PyArrow type and write a real
   Parquet file (a controllable page size / page version can be forced so the
   file actually spans multiple pages).
2. Read the file back through PyArrow and return its Python value tree -- the
   *value-level* external oracle for the kernel round trip.

The page-level (definition/repetition level) external oracle lives in
:mod:`app.adapters.level_oracle`; PyArrow itself does not expose page level
arrays from Python, so that oracle parses the file with fastparquet.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from ..core.errors import ErrorCode, error
from ..core.schema import Node, Schema

_DSL_TO_ARROW_PRIMITIVE: dict[str, pa.DataType] = {
    "int32": pa.int32(),
    "int64": pa.int64(),
    "boolean": pa.bool_(),
    "double": pa.float64(),
    "string": pa.string(),
    "binary": pa.binary(),
}


def _to_arrow_type(node: Node) -> pa.DataType:
    if node.kind == "primitive":
        return _DSL_TO_ARROW_PRIMITIVE[node.primitive]  # type: ignore[index]
    if node.kind == "list":
        assert node.item is not None
        # Arrow field names mirror the canonical list.element group.
        return pa.list_(_to_arrow_field(node.item, "element"))
    if node.kind == "struct":
        return pa.struct([_to_arrow_field(c, c.name) for c in node.children])
    raise error(ErrorCode.UNSUPPORTED_LOGICAL_TYPE,
                f"cannot map node kind {node.kind!r} to PyArrow")


def _to_arrow_field(node: Node, name: Optional[str] = None) -> pa.Field:
    field_name = name or node.name
    return pa.field(field_name, _to_arrow_type(node), nullable=node.nullable)


def build_arrow_schema(schema: Schema) -> pa.Schema:
    return pa.schema([_to_arrow_field(c, c.name) for c in schema.root.children])


def _column_for(node: Node, values: list[Any]) -> pa.Array:
    if node.kind == "primitive":
        atype = _DSL_TO_ARROW_PRIMITIVE[node.primitive]
        return pa.array(values, type=atype)
    if node.kind == "list":
        assert node.item is not None
        return _build_list(node, values)
    if node.kind == "struct":
        per_child: dict[str, list[Any]] = {c.name: [] for c in node.children}
        for v in values:
            if v is None:
                for c in node.children:
                    per_child[c.name].append(None)
            else:
                for c in node.children:
                    per_child[c.name].append(v.get(c.name))
        arrays = [_column_for(c, per_child[c.name]) for c in node.children]
        fields = [pa.field(c.name, _to_arrow_type(c), nullable=c.nullable)
                  for c in node.children]
        return pa.StructArray.from_arrays(arrays, fields=fields)
    raise AssertionError(node.kind)


def _build_list(node: Node, values: list[Any]) -> pa.Array:
    """Build an Arrow list array from Python lists, preserving NULL lists."""
    offsets = [0]
    flat: list[Any] = []
    nulls: list[bool] = []
    for v in values:
        if v is None:
            nulls.append(True)
            offsets.append(offsets[-1])
        else:
            nulls.append(False)
            offsets.append(offsets[-1] + len(v))
            flat.extend(v)
    assert node.item is not None
    child = _column_for(node.item, flat)
    offs = pa.array(offsets, type=pa.int32())
    return pa.ListArray.from_arrays(
        offs, child,
        type=pa.list_(_to_arrow_field(node.item, "element")),
        mask=pa.array(nulls, type=pa.bool_()),
    )


def build_arrow_table(schema: Schema, records: list[dict[str, Any]]) -> pa.Table:
    cols = []
    fields = []
    for child in schema.root.children:
        vals = [rec.get(child.name) if isinstance(rec, dict) else None
                for rec in records]
        arr = _column_for(child, vals)
        cols.append(arr)
        fields.append(pa.field(child.name, arr.type, nullable=child.nullable))
    return pa.Table.from_arrays(cols, schema=pa.schema(fields))


def write_parquet(
    schema: Schema,
    records: list[dict[str, Any]],
    path: Path,
    *,
    data_page_size: int = 1024,
    page_version: str = "2.0",
    compression: str = "NONE",
) -> Path:
    table = build_arrow_table(schema, records)
    pq.write_table(
        table, str(path),
        compression=compression,
        use_dictionary=False,
        write_statistics=False,
        data_page_size=data_page_size,
        data_page_version=page_version,
    )
    return path


def read_parquet_values(path: Path) -> dict[str, list[Any]]:
    return pq.read_table(str(path)).to_pydict()
