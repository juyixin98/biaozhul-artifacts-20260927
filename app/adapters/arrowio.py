"""PyArrow <-> kernel adapter.

Input contract (Arrow IPC stream, one RecordBatch per "batch"):

* exactly one column, named by the caller (default ``value``);
* the column MUST be a ``DictionaryArray`` whose dictionary has one of the
  supported value types and whose indices are a signed OR unsigned int (they are
  checked range-wise regardless);
* NULL rows are carried by the index array's own null bitmap — Arrow encodes
  dictionary nulls as null *indices*, which maps directly onto our validity
  bitmap; a NULL entry inside the *dictionary* itself is rejected
  (``NULL_DICTIONARY_ENTRY``).

Output contract: an Arrow IPC stream of one RecordBatch, containing one
DictionaryArray column per input batch (columns named after batch ids), all
sharing the global dictionary and the selected unsigned index width.
"""
from __future__ import annotations

import pyarrow as pa

from ..core.errors import (
    MalformedBatchError,
    NullDictionaryEntryError,
    UnsupportedValueTypeError,
)
from ..core.kernel import BatchInput, UnifyResult

_PA_VALUE_TYPES: dict[pa.DataType, str] = {
    pa.string(): "string",
    pa.large_string(): "string",
    pa.utf8(): "string",
    pa.int64(): "int64",
    pa.float64(): "double",
    pa.bool_(): "bool",
}

_PA_FROM_VALUE_TYPE: dict[str, pa.DataType] = {
    "string": pa.string(),
    "int64": pa.int64(),
    "double": pa.float64(),
    "bool": pa.bool_(),
}

_PA_INDEX_TYPE: dict[int, pa.DataType] = {
    8: pa.uint8(),
    16: pa.uint16(),
    32: pa.uint32(),
}


def pa_value_type_name(t: pa.DataType) -> str:
    # Dictionary value type equality: compare via string identity of dtype
    # names plus structural compatibility (large_string -> string).
    for candidate, name in _PA_VALUE_TYPES.items():
        if t == candidate:
            return name
    raise UnsupportedValueTypeError(
        "unsupported Arrow dictionary value type",
        details={"arrow_type": str(t)},
    )


def _safe_pylist(arr: pa.Array) -> list:
    """to_pylist that treats a NULL inside a *dictionary* as an error."""
    if arr.null_count:
        raise NullDictionaryEntryError(
            "NULL entries inside the dictionary values are not legal; "
            "encode NULL rows through the index bitmap",
            details={"null_entries": arr.null_count},
        )
    return arr.to_pylist()


def dictionary_array_to_batch(
    col: pa.DictionaryArray,
    *,
    batch_id: str,
    value_type: str,
) -> BatchInput:
    """Convert one Arrow DictionaryArray column to a kernel BatchInput."""
    if not pa.types.is_dictionary(col.type):
        raise MalformedBatchError(
            "Arrow column must be dictionary-encoded",
            details={"batch_id": batch_id, "arrow_type": str(col.type)},
        )
    dict_name = pa_value_type_name(col.type.value_type)
    if dict_name != value_type:
        raise UnsupportedValueTypeError(
            "Arrow dictionary value type disagrees with declared value_type",
            details={"batch_id": batch_id, "arrow_value_type": dict_name,
                     "declared": value_type},
        )

    dictionary = _safe_pylist(col.dictionary)
    index_arr = col.indices
    if not pa.types.is_integer(index_arr.type):
        raise MalformedBatchError(
            "dictionary indices must be integers",
            details={"batch_id": batch_id, "index_type": str(index_arr.type)},
        )

    raw_indices = index_arr.to_pylist()
    # Arrow null indices => validity False (NULL occupies no value code).
    validity = [idx is not None for idx in raw_indices]
    indices = [idx if idx is not None else 0 for idx in raw_indices]

    return BatchInput(
        batch_id=batch_id,
        dictionary=dictionary,
        indices=indices,
        validity=validity,
    )


def decode_ipc_batches(buf: bytes, *, value_type: str,
                      column: str = "value") -> list[BatchInput]:
    """Parse an Arrow IPC stream into kernel batches.

    RecordBatch metadata ``batch_id`` (UTF-8) is used when present; otherwise
    batches are named ``batch-0``, ``batch-1`` ... in stream order.
    """
    reader = pa.ipc.open_stream(pa.BufferReader(buf))
    schema = reader.schema
    if column not in schema.names:
        raise MalformedBatchError(
            "requested column not found in Arrow stream",
            details={"column": column, "available": schema.names},
        )
    if len(schema.names) != 1:
        raise MalformedBatchError(
            "Arrow stream must contain exactly one column",
            details={"columns": schema.names},
        )

    batches: list[BatchInput] = []
    for ordinal, record_batch in enumerate(reader):
        meta = record_batch.schema.metadata or {}
        bid = meta.get(b"batch_id")
        batch_id = bid.decode("utf-8") if bid is not None else f"batch-{ordinal}"
        col = record_batch.column(0)
        batches.append(
            dictionary_array_to_batch(col, batch_id=batch_id, value_type=value_type)
        )
    return batches


def result_to_ipc(result: UnifyResult) -> bytes:
    """Serialize the result as an Arrow IPC stream.

    One column per batch (named after batch_id); every column is a
    DictionaryArray over the same global dictionary using unsigned indices of
    the selected width. NULL rows are null indices.
    """
    value_pa_type = _PA_FROM_VALUE_TYPE[result.global_value_type]
    dictionary = pa.array(list(result.global_dictionary), type=value_pa_type)
    index_type = _PA_INDEX_TYPE[result.index_width_bits]

    arrays: dict[str, pa.DictionaryArray] = {}
    for remap in result.batch_remaps:
        idx_pylist = [
            code if valid else None
            for code, valid in zip(remap.global_indices, remap.validity)
        ]
        indices = pa.array(idx_pylist, type=index_type)
        arr = pa.DictionaryArray.from_arrays(indices, dictionary)
        arrays[remap.batch_id] = arr

    table = pa.table(arrays)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()
