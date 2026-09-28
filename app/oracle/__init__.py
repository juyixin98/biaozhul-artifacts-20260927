"""PyArrow oracle bridge.

PyArrow is used strictly as an *independent* reference implementation: it
builds its own Arrow arrays from the same JSON records, writes/reads its own
Parquet bytes, and returns plain-Python trees. The kernel under test never
shares code or expected answers with this module -- the test fixtures'
expected trees are hand written, and both the self-implemented codec and
PyArrow are compared against them and against each other.
"""
from __future__ import annotations

from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from ..kernel.schema import (
    ListNode, OPTIONAL, PhysicalType, PrimitiveNode, REQUIRED, RootNode,
    StructNode,
)


class OracleError(RuntimeError):
    pass


_PHYSICAL_ARROW = {
    PhysicalType.BOOLEAN: pa.bool_(),
    PhysicalType.INT32: pa.int32(),
    PhysicalType.INT64: pa.int64(),
    PhysicalType.FLOAT: pa.float32(),
    PhysicalType.DOUBLE: pa.float64(),
    PhysicalType.BYTE_ARRAY: pa.string(),
}


def _arrow_field(node, nullable_default=True) -> pa.Field:
    if isinstance(node, PrimitiveNode):
        return pa.field(node.name, _PHYSICAL_ARROW[node.physical],
                        nullable=(node.repetition != REQUIRED))
    if isinstance(node, StructNode):
        children = [_arrow_field(c) for c in node.fields]
        return pa.field(node.name, pa.struct(children),
                        nullable=(node.repetition != REQUIRED))
    if isinstance(node, ListNode):
        value_field = _arrow_field(node.element)
        value_type = value_field.type
        return pa.field(node.name, pa.list_(value_type),
                        nullable=(node.repetition != REQUIRED))
    raise OracleError(f"cannot map node {node!r}")


def arrow_schema(root: RootNode) -> pa.Schema:
    return pa.schema([_arrow_field(f) for f in root.fields])


def write_reference_parquet(path: str, root: RootNode,
                            records: list[dict[str, Any]],
                            page_size_bytes: int | None = None) -> str:
    """Have PyArrow independently write the records to ``path``."""
    schema = arrow_schema(root)
    # Build column-by-column so missing keys behave like NULL uniformly.
    arrays = {}
    for field in root.fields:
        values = [rec.get(field.name) for rec in records]
        arrays[field.name] = pa.array(values, type=schema.field(field.name).type)
    table = pa.table(arrays, schema=schema)
    kwargs = dict(
        data_page_version="1.0",
        use_dictionary=False,
        write_statistics=False,
        compression="none",
    )
    if page_size_bytes is not None:
        # Small target forces Arrow itself to emit multiple pages, exercising
        # cross-page reassembly in our reader.
        kwargs["data_page_size"] = page_size_bytes
        kwargs["write_batch_size"] = max(1, page_size_bytes // 4)
    pq.write_table(table, path, **kwargs)
    return path


def read_reference_parquet(path: str) -> list[dict[str, Any]]:
    return pq.read_table(path).to_pylist()


def reference_roundtrip(root: RootNode, records: list[dict[str, Any]],
                        page_size_bytes: int | None = None) -> list[dict[str, Any]]:
    import tempfile
    import os
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "oracle.parquet")
        write_reference_parquet(p, root, records, page_size_bytes)
        return read_reference_parquet(p)
