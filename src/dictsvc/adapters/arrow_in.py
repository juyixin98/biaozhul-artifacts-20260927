"""Arrow IPC input adapter.

Accepts an Arrow IPC stream of dictionary-encoded columns. Each column is
one batch; the batch id is taken from the column metadata key ``batch_id``
when present, otherwise the column name. The local index width is read
from the dictionary indices (int8/16/32/64) and is independent of the
column's null bitmap -- a null index bit means NULL, regardless of the
numeric slot sitting in the indices buffer.
"""
from __future__ import annotations

import pyarrow as pa

from ..core.errors import RequestMalformed, UnsupportedValueType
from ..core.model import BatchInput
from ..core.policy import NULL_SENTINEL

BATCH_ID_KEY = b"batch_id"


def parse_arrow_stream(data: bytes) -> list[BatchInput]:
    try:
        reader = pa.ipc.open_stream(data)
        record_batches = list(reader)
        schema = reader.schema
    except Exception as exc:
        raise RequestMalformed(
            f"payload is not an Arrow IPC stream: {exc}") from exc

    batches: list[BatchInput] = []
    # Preserve column order; concatenate columns across record batches.
    for i in range(len(schema)):
        field = schema.field(i)
        chunks = [rb.column(i) for rb in record_batches]
        if not chunks:
            continue
        arr = (chunks[0] if len(chunks) == 1
               else pa.chunked_array(chunks, type=field.type)
               .combine_chunks())
        meta = field.metadata or {}
        batch_id = meta[BATCH_ID_KEY].decode() if BATCH_ID_KEY in meta \
            else field.name
        batches.append(_parse_column(batch_id, arr))
    if not batches:
        raise RequestMalformed("Arrow payload contained no columns")
    return batches


def _parse_column(batch_id: str, arr: pa.Array) -> BatchInput:
    dtype = arr.type
    if not pa.types.is_dictionary(dtype):
        raise RequestMalformed(
            f"column {batch_id!r} must be dictionary-encoded, got {dtype}",
            batch_id=batch_id)
    vt = dtype.value_type
    if not (pa.types.is_string(vt) or pa.types.is_large_string(vt)
            or pa.types.is_int64(vt)):
        raise UnsupportedValueType(
            f"column {batch_id!r}: dictionary value type {vt} not supported; "
            f"use int64 or utf8",
            batch_id=batch_id)

    values = tuple(arr.dictionary.to_pylist())
    raw_indices = arr.indices.to_pylist()
    indices = tuple(NULL_SENTINEL if idx is None else idx
                    for idx in raw_indices)
    valid = tuple(idx is not None for idx in raw_indices)
    return BatchInput(batch_id=batch_id, values=values,
                      indices=indices, valid=valid)
