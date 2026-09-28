"""Ingestion, schema management and rewrite/compaction service.

Ingestion never reassigns row identities: the catalog hands out a contiguous
block of ``__row_id`` values before the chunk is encoded, and a rewrite reads
the existing ``__row_id`` column and carries it verbatim into the new chunk
files. Only chunk ids are fresh after a rewrite; old chunk files are deleted
only after the catalog commit that atomically swaps the records.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import Catalog, ChunkRecord
from .chunkstore import ROW_ID_COL, build_sorted_table, delete_chunk, read_table, write_chunk
from .encoding import DimSpec, SchemaSpec
from .errors import InvalidCoordinate
from .fixtures import generate_rows
from .logging_setup import get_logger

log = get_logger("ingest")


@dataclass
class IngestResult:
    schema: str
    rows_ingested: int
    chunk_ids: list[int]
    chunk_summaries: list[dict[str, Any]]


class IngestService:
    def __init__(self, catalog: Catalog, data_dir: str | Path, default_capacity: int = 8192) -> None:
        self.catalog = catalog
        self.data_dir = Path(data_dir)
        self.default_capacity = default_capacity

    # ------------------------------------------------------------------ schema
    def create_schema(self, name: str, dims: list[dict], *, overwrite: bool = False) -> SchemaSpec:
        spec = SchemaSpec(
            name=name,
            dims=tuple(DimSpec(d["name"], int(d["bits"]), bool(d.get("signed", True))) for d in dims),
        )
        if overwrite:
            # destructive: wipe registered chunk files and metadata
            existing = self.catalog.list_chunks(name)
            for rec in existing:
                delete_chunk(self.data_dir, name, rec.chunk_id)
            self.catalog.delete_schema_metadata(name)
        self.catalog.put_schema(spec, overwrite=overwrite)
        log.info("schema_created", extra={"data": {"schema": name, "dims": spec.to_dict()["dims"], "overwrite": overwrite}})
        return spec

    def replace_schema(self, name: str, dims: list[dict]) -> SchemaSpec:
        # Require the schema to already exist (PUT = replace, not create).
        self.catalog.get_schema(name)
        return self.create_schema(name, dims, overwrite=True)

    # ------------------------------------------------------------------ ingest
    def _validate_rows(self, schema: SchemaSpec, rows: list[list[int] | tuple[int, ...]]) -> None:
        for i, row in enumerate(rows):
            if len(row) != len(schema.dims):
                raise InvalidCoordinate(
                    f"row {i}: expected {len(schema.dims)} coordinates, got {len(row)}",
                    row_index=i,
                )
            for dim, value in zip(schema.dims, row):
                if not isinstance(value, int) or isinstance(value, bool):
                    raise InvalidCoordinate(
                        f"row {i}, dim {dim.name!r}: value must be an integer",
                        row_index=i,
                        dim=dim.name,
                        value=repr(value),
                    )
                from .encoding import to_unsigned

                try:
                    to_unsigned(value, dim)
                except ValueError as exc:
                    raise InvalidCoordinate(str(exc), row_index=i, dim=dim.name, value=value) from exc

    def ingest_rows(
        self,
        name: str,
        rows: list[list[int]] | list[tuple[int, ...]],
        capacity: int | None = None,
    ) -> IngestResult:
        schema = self.catalog.get_schema(name)
        self._validate_rows(schema, rows)
        cap = capacity or self.default_capacity
        if cap < 1:
            raise ValueError("chunk capacity must be >= 1")

        chunk_summaries: list[dict[str, Any]] = []
        chunk_ids: list[int] = []
        for start in range(0, len(rows), cap):
            block = [tuple(r) for r in rows[start:start + cap]]
            if not block:
                continue
            reservation = self.catalog.reserve_ids(len(block))
            table = build_sorted_table(schema, list(reservation.row_ids), block)
            written = write_chunk(self.data_dir, name, reservation.chunk_id, table)
            self.catalog.register_chunk(ChunkRecord(
                chunk_id=written.chunk_id,
                schema=name,
                path=str(written.path),
                num_rows=written.num_rows,
                min_code=written.min_code,
                max_code=written.max_code,
                byte_size=written.byte_size,
            ))
            chunk_ids.append(written.chunk_id)
            chunk_summaries.append({
                "chunk_id": written.chunk_id,
                "num_rows": written.num_rows,
                "min_code": str(written.min_code),
                "max_code": str(written.max_code),
                "byte_size": written.byte_size,
            })
        log.info("rows_ingested", extra={"data": {
            "schema": name, "rows": len(rows), "chunks": chunk_ids,
        }})
        return IngestResult(name, len(rows), chunk_ids, chunk_summaries)

    def ingest_synthetic(
        self,
        name: str,
        n: int,
        shape: str = "uniform",
        seed: int = 1,
        capacity: int | None = None,
    ) -> IngestResult:
        schema = self.catalog.get_schema(name)
        rows = generate_rows(schema, n, shape=shape, seed=seed)
        return self.ingest_rows(name, rows, capacity=capacity)

    # ----------------------------------------------------------------- rewrite
    def rewrite_all(self, name: str, capacity: int | None = None) -> dict[str, Any]:
        """Read every chunk of the schema and rewrite into fresh chunks.

        ``__row_id`` values are preserved exactly; chunk ids are newly
        reserved and the catalog swap is one atomic transaction.
        """
        schema = self.catalog.get_schema(name)
        records = self.catalog.list_chunks(name)
        if not records:
            return {"schema": name, "old_chunks": [], "new_chunks": [], "rows_rewritten": 0}
        cap = capacity or self.default_capacity

        # Stable row identity comes straight from the existing files; the
        # rewrite's job is to re-order by Morton code for disjoint chunks.
        rid_rows: list[tuple[int, tuple[int, ...]]] = []
        for rec in records:
            table = read_table(rec.path, rec.chunk_id)
            rids = table.column(ROW_ID_COL).to_pylist()
            cols = [table.column(d.name).to_pylist() for d in schema.dims]
            for i, rid in enumerate(rids):
                rid_rows.append((rid, tuple(cols[j][i] for j in range(len(schema.dims)))))
        # Global Morton re-sort; ties broken by stable row id.
        from .encoding import encode
        rid_rows.sort(key=lambda t: (encode(t[1], schema), t[0]))

        new_records: list[ChunkRecord] = []
        num_new = (len(rid_rows) + cap - 1) // cap
        new_chunk_ids = self.catalog.reserve_chunk_ids(num_new)
        for cid, start in zip(new_chunk_ids, range(0, len(rid_rows), cap)):
            block = rid_rows[start:start + cap]
            stable_ids = [rid for rid, _ in block]
            raw_rows = [row for _, row in block]
            table = build_sorted_table(schema, stable_ids, raw_rows)
            written = write_chunk(self.data_dir, name, cid, table)
            new_records.append(ChunkRecord(
                chunk_id=written.chunk_id,
                schema=name,
                path=str(written.path),
                num_rows=written.num_rows,
                min_code=written.min_code,
                max_code=written.max_code,
                byte_size=written.byte_size,
            ))

        old_ids = [r.chunk_id for r in records]
        self.catalog.replace_chunks(name, old_ids, new_records)
        # Only after the metadata commit do old files become unreachable.
        for cid in old_ids:
            delete_chunk(self.data_dir, name, cid)
        result = {
            "schema": name,
            "rows_rewritten": len(rid_rows),
            "old_chunks": old_ids,
            "new_chunks": [
                {
                    "chunk_id": r.chunk_id,
                    "num_rows": r.num_rows,
                    "min_code": str(r.min_code),
                    "max_code": str(r.max_code),
                    "byte_size": r.byte_size,
                }
                for r in new_records
            ],
            "row_id_preserved": True,
        }
        log.info("chunks_rewritten", extra={"data": result})
        return result
