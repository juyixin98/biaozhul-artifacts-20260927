"""Local columnar chunk storage (PyArrow IPC files).

On-disk layout per schema under ``<data_dir>/chunks/<schema>/``::

    chunk_<cid>.arrow   - Arrow IPC (uncompressed, file format)
    chunk_<cid>.meta    - JSON: schema name, layout version, row count, min/max

Each chunk holds the columns::

    __row_id        uint64, stable across rewrites
    __mc_hi         uint64, Morton code high 64 bits
    __mc_lo         uint64, Morton code low 64 bits
    <dim name...>   int64, original raw coordinates (used for exact filter)

Rows are sorted by (hi, lo) Morton code ascending, so one chunk's codes span
``[min_code, max_code]`` and a code-range overlap test is enough for pruning.
Files are written to a temp name and atomically renamed, and byte sizes are
read from ``stat()`` so reported I/O reflects real local reads.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc as ipc

from .encoding import MAX_TOTAL_BITS, SchemaSpec, combine128, encode

LAYOUT_VERSION = "columnar-v1"
ROW_ID_COL = "__row_id"
MC_HI_COL = "__mc_hi"
MC_LO_COL = "__mc_lo"
RESERVED = {ROW_ID_COL, MC_HI_COL, MC_LO_COL}


class ChunkUnreadable(Exception):
    """Raised when a registered chunk file is missing/corrupt."""

    def __init__(self, chunk_id: int, path: Path, reason: str) -> None:
        super().__init__(f"chunk {chunk_id} at {path} unreadable: {reason}")
        self.chunk_id = chunk_id
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class WrittenChunk:
    chunk_id: int
    path: Path
    num_rows: int
    min_code: int
    max_code: int
    byte_size: int
    min_coords: tuple[int, ...]
    max_coords: tuple[int, ...]


def chunk_dir(data_dir: str | Path, schema_name: str) -> Path:
    return Path(data_dir) / "chunks" / schema_name


def chunk_path(data_dir: str | Path, schema_name: str, chunk_id: int) -> Path:
    return chunk_dir(data_dir, schema_name) / f"chunk_{chunk_id:010d}.arrow"


def _meta_path(path: Path) -> Path:
    return path.with_suffix(".meta")


def _arrow_schema(schema: SchemaSpec) -> pa.Schema:
    fields = [
        pa.field(ROW_ID_COL, pa.uint64(), nullable=False),
        pa.field(MC_HI_COL, pa.uint64(), nullable=False),
        pa.field(MC_LO_COL, pa.uint64(), nullable=False),
    ]
    for d in schema.dims:
        fields.append(pa.field(d.name, pa.int64(), nullable=False))
    return pa.schema(fields)


def build_sorted_table(
    schema: SchemaSpec,
    row_ids: list[int],
    rows: list[tuple[int, ...]],
) -> pa.Table:
    """Encode + sort rows by Morton code. ``row_ids`` travel with their rows."""
    if len(row_ids) != len(rows):
        raise ValueError("row_ids / rows length mismatch")
    if not rows:
        raise ValueError("refusing to write an empty chunk")
    encoded: list[tuple[int, int, int, tuple[int, ...]]] = []
    for rid, coords in zip(row_ids, rows):
        if len(coords) != len(schema.dims):
            raise ValueError(f"row {rid}: expected {len(schema.dims)} coords")
        code = encode(tuple(int(c) for c in coords), schema)
        if code >> MAX_TOTAL_BITS:
            raise AssertionError("Morton code exceeded storage width (schema validation bug)")
        hi = code >> 64
        lo = code & ((1 << 64) - 1)
        encoded.append((hi, lo, rid, tuple(int(c) for c in coords)))
    encoded.sort(key=lambda t: (t[0], t[1], t[2]))
    n = len(encoded)
    cols: dict[str, pa.Array] = {
        ROW_ID_COL: pa.array([t[2] for t in encoded], type=pa.uint64()),
        MC_HI_COL: pa.array([t[0] for t in encoded], type=pa.uint64()),
        MC_LO_COL: pa.array([t[1] for t in encoded], type=pa.uint64()),
    }
    for j, d in enumerate(schema.dims):
        cols[d.name] = pa.array([t[3][j] for t in encoded], type=pa.int64())
    return pa.table(cols, schema=_arrow_schema(schema))


def write_chunk(
    data_dir: str | Path,
    schema_name: str,
    chunk_id: int,
    table: pa.Table,
) -> WrittenChunk:
    """Atomically write a prebuilt sorted table as chunk ``chunk_id``."""
    directory = chunk_dir(data_dir, schema_name)
    directory.mkdir(parents=True, exist_ok=True)
    target = chunk_path(data_dir, schema_name, chunk_id)

    his = table.column(MC_HI_COL).to_pylist()
    los = table.column(MC_LO_COL).to_pylist()
    codes_sorted = sorted(combine128(h, l) for h, l in zip(his, los))
    min_code, max_code = codes_sorted[0], codes_sorted[-1]
    dim_names = [f.name for f in table.schema if f.name not in RESERVED]
    min_coords = tuple(min(table.column(name).to_pylist()) for name in dim_names)
    max_coords = tuple(max(table.column(name).to_pylist()) for name in dim_names)

    fd, tmp_name = tempfile.mkstemp(prefix=f".chunk_{chunk_id:010d}.", suffix=".tmp", dir=directory)
    os.close(fd)
    try:
        with pa.OSFile(tmp_name, "wb") as sink:
            with ipc.new_file(sink, table.schema) as writer:
                writer.write_table(table)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

    meta = {
        "layout": LAYOUT_VERSION,
        "schema": schema_name,
        "chunk_id": chunk_id,
        "num_rows": table.num_rows,
        "min_code": str(min_code),
        "max_code": str(max_code),
        "min_coords": list(min_coords),
        "max_coords": list(max_coords),
    }
    with open(_meta_path(target), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)

    return WrittenChunk(
        chunk_id=chunk_id,
        path=target,
        num_rows=table.num_rows,
        min_code=min_code,
        max_code=max_code,
        byte_size=target.stat().st_size,
        min_coords=min_coords,
        max_coords=max_coords,
    )


def delete_chunk(data_dir: str | Path, schema_name: str, chunk_id: int) -> None:
    for p in (chunk_path(data_dir, schema_name, chunk_id), _meta_path(chunk_path(data_dir, schema_name, chunk_id))):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def read_table(path: str | Path, chunk_id: int) -> pa.Table:
    """Read a chunk fully into memory.

    We deliberately open (not mmap) and materialize the table before the file
    handle closes: handing back a Table backed by an mmap whose mapping has
    already been unmapped is undefined and crashed the native process on
    repeated reads during rewrite validation.
    """
    p = Path(path)
    try:
        with pa.OSFile(str(p), "r") as source:
            try:
                reader = ipc.RecordBatchFileReader(source)
            except pa.ArrowInvalid as exc:
                raise ChunkUnreadable(chunk_id, p, f"not an Arrow IPC file: {exc}") from exc
            return reader.read_all()
    except (FileNotFoundError, OSError) as exc:
        raise ChunkUnreadable(chunk_id, p, str(exc)) from exc


def byte_size(path: str | Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0
