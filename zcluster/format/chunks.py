"""Columnar chunk format on top of Apache Arrow IPC.

A chunk is one Arrow IPC file containing, in order:

* ``row_id``   uint64        - stable identity, preserved across rewrites
* ``code``     uint64        - Morton code, when ``total_bits <= 64``
  ``code``     binary[n]     - fixed-width big-endian code otherwise
* one column per dimension, in coder order, using the smallest Arrow integer
  type that holds the fixed width (int8/16/32/64 for signed, uint* for not)

Rows are sorted by ascending code: the code column is the clustering key and
is what range scans and interval bisect operations work against.  Schema
metadata records the format/coder versions, dimension specs and code encoding,
so a file is self-describing.
"""

from __future__ import annotations

import os

import pyarrow as pa
import pyarrow.ipc as ipc

from ..kernel.coder import FORMAT_VERSION, DimSpec, MortonCoder

CHUNK_FORMAT_VERSION = 1
CODE_COLUMN = "code"
ROW_ID_COLUMN = "row_id"


def _arrow_integer_type(dim: DimSpec) -> pa.DataType:
    signed = dim.signed
    if dim.bits <= 8:
        return pa.int8() if signed else pa.uint8()
    if dim.bits <= 16:
        return pa.int16() if signed else pa.uint16()
    if dim.bits <= 32:
        return pa.int32() if signed else pa.uint32()
    return pa.int64() if signed else pa.uint64()


def code_arrow_type(coder: MortonCoder) -> pa.DataType:
    if coder.fits_uint64:
        return pa.uint64()
    return pa.binary(coder.code_byte_width)


def build_arrow_schema(coder: MortonCoder) -> pa.Schema:
    fields = [
        pa.field(ROW_ID_COLUMN, pa.uint64(), nullable=False),
        pa.field(CODE_COLUMN, code_arrow_type(coder), nullable=False),
    ]
    for d in coder.dims:
        fields.append(pa.field(d.name, _arrow_integer_type(d), nullable=False))
    md = {
        b"format_version": str(CHUNK_FORMAT_VERSION).encode(),
        b"coder_version": str(FORMAT_VERSION).encode(),
        b"dimensions": __import__("json").dumps(coder.dims_to_dict()).encode(),
        b"code_storage": b"uint64" if coder.fits_uint64 else b"fixed_binary",
        b"code_byte_width": str(coder.code_byte_width).encode(),
    }
    return pa.schema(fields, metadata=md)


def _encode_code_column(coder: MortonCoder, rows: list[tuple[int, list[int]]]) -> pa.Array:
    if coder.fits_uint64:
        return pa.array(
            [coder.encode(coords) for _, coords in rows], type=pa.uint64()
        )
    w = coder.code_byte_width
    return pa.array(
        [coder.encode(coords).to_bytes(w, "big") for _, coords in rows],
        type=pa.binary(w),
    )


def build_table(coder: MortonCoder, rows: list[tuple[int, list[int]]]) -> pa.Table:
    """Build a code-sorted Arrow table from ``(row_id, raw_coords)`` rows."""
    ordered = sorted(rows, key=lambda r: coder.encode(r[1]))
    arrays = {
        ROW_ID_COLUMN: pa.array([r[0] for r in ordered], type=pa.uint64()),
        CODE_COLUMN: _encode_code_column(coder, ordered),
    }
    for i, d in enumerate(coder.dims):
        arrays[d.name] = pa.array(
            [r[1][i] for r in ordered], type=_arrow_integer_type(d)
        )
    return pa.Table.from_pydict(arrays, schema=build_arrow_schema(coder))


def write_chunk(path: str, table: pa.Table, compression: str = "zstd") -> None:
    """Atomically write an Arrow IPC file (tmp file + fsync + rename)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    opts = ipc.IpcWriteOptions(compression=compression)
    with pa.OSFile(tmp, "wb") as sink:
        with ipc.new_file(sink, table.schema, options=opts) as writer:
            writer.write_table(table)
        sink.flush()
    os.replace(tmp, path)


def read_chunk(path: str, columns: list[str] | None = None) -> pa.Table:
    with pa.memory_map(path, "r") as source:
        with ipc.RecordBatchFileReader(source) as reader:
            table = reader.read_all()
    if columns is not None:
        table = table.select(columns)
    return table


def chunk_schema_metadata(path: str) -> dict:
    with pa.memory_map(path, "r") as source:
        with ipc.RecordBatchFileReader(source) as reader:
            return dict(reader.schema.metadata or {})


# -- code-column access helpers ---------------------------------------------

def code_column_as_int(table: pa.Table, coder: MortonCoder) -> list[int]:
    """Return the code column as exact Python ints regardless of storage."""
    col = table.column(CODE_COLUMN)
    if coder.fits_uint64:
        return [int(v) for v in col.to_pylist()]
    return [int.from_bytes(b, "big") for b in col.to_pylist()]


def codes_intersect_lo_hi(code_lo: int, code_hi: int, cmin: int, cmax: int) -> bool:
    return not (code_hi < cmin or code_lo > cmax)
