"""Arrow IPC output adapter.

The remapped rows of all batches are emitted as one *vertical* table --
Arrow tables are rectangular, so batches of different lengths are stacked
rather than placed side by side:

    batch_id:    utf8   (source batch)
    global_code: int64  (null at NULL rows; validity bitmap independent)
    valid:       bool   (the independent NULL bitmap)

The global dictionary itself is delivered as JSON, because one Arrow
column cannot carry heterogeneous value types (int64 + utf8) in a single
dictionary.
"""
from __future__ import annotations

import pyarrow as pa

from ..core.model import GlobalEncoding


def build_remap_stream(enc: GlobalEncoding) -> bytes:
    batch_ids: list[str] = []
    codes: list[int | None] = []
    valid: list[bool] = []
    for rb in enc.batches:
        for c, v in zip(rb.global_indices, rb.valid):
            batch_ids.append(rb.batch_id)
            codes.append(c if v else None)
            valid.append(bool(v))

    schema = pa.schema([
        pa.field("batch_id", pa.utf8(), nullable=False),
        pa.field("global_code", pa.int64(), nullable=True),
        pa.field("valid", pa.bool_(), nullable=False),
    ])
    table = pa.table({
        "batch_id": pa.array(batch_ids, type=pa.utf8()),
        "global_code": pa.array(codes, type=pa.int64()),
        "valid": pa.array(valid, type=pa.bool_()),
    }, schema=schema)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()
