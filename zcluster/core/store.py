"""Execution core: ingest, code-clustered chunks, range query, compaction.

The query path is deliberately two-stage:

1. **Conservative** — the box is decomposed into Morton intervals; every chunk
   whose ``[code_min, code_max]`` touches any interval is read, and within each
   chunk every code inside any interval is selected (candidate set).
2. **Exact** — candidate coordinates are decoded-compared (raw coordinates,
   unsigned box edges) and rows outside the box are dropped.

When the interval budget is exhausted the decomposition falls back to the full
domain, so the conservative stage can only inflate, never miss.  All inflation
is reported back per query (candidate/result counts, chunks read, bytes read).
"""

from __future__ import annotations

import os
import uuid
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

import pyarrow as pa

from ..errors import (
    AlreadyInitializedError,
    BudgetError,
    CoordinateError,
    NotInitializedError,
    QueryValidationError,
    SchemaValidationError,
)
from ..format.chunks import (
    CHUNK_FORMAT_VERSION,
    CODE_COLUMN,
    ROW_ID_COLUMN,
    build_table,
    chunk_schema_metadata,
    code_column_as_int,
    read_chunk,
    write_chunk,
)
from ..kernel.coder import DimSpec, MortonCoder, OutOfDomainError
from ..kernel.decompose import decompose_box, point_in_box_unsigned
from ..logging_setup import get_logger
from ..meta.catalog import Catalog, ChunkRecord

log = get_logger("store")
CHUNKS_SUBDIR = "chunks"


@dataclass
class QueryOutcome:
    request_id: str
    rows: list[dict]
    box: dict
    budget: int
    budget_exhausted: bool
    intervals: list[dict]
    stats: dict
    steps: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)


class Store:
    """Owns the catalog, coder and chunk directory for one data root."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.root = cfg.data_root
        os.makedirs(os.path.join(self.root, CHUNKS_SUBDIR), exist_ok=True)
        self.catalog = Catalog(self.root)
        self.coder: MortonCoder | None = None
        self._dim_specs: list[DimSpec] = []
        row = self.catalog.dataset_row()
        if row is not None:
            self._load_schema(row)

    def close(self) -> None:
        self.catalog.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- schema -----------------------------------------------------------
    def _load_schema(self, row) -> None:
        dims = __import__("json").loads(row["dimensions_json"])
        self._dim_specs = [DimSpec.from_dict(d) for d in dims]
        self.coder = MortonCoder(self._dim_specs)

    def initialize(self, name: str, dimensions: list[dict], request_id: str) -> dict:
        if self.catalog.dataset_row() is not None:
            raise AlreadyInitializedError("dataset is already initialized")
        try:
            specs = [DimSpec.from_dict(d) for d in dimensions]
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaValidationError(f"invalid dimension spec: {exc}") from exc
        coder = MortonCoder(specs)
        self.catalog.initialize_dataset(
            name=name, dims=[d.to_dict() for d in specs],
            chunk_size=self.cfg.chunk_size,
            coder_version=1, chunk_format_version=CHUNK_FORMAT_VERSION,
        )
        self._dim_specs = specs
        self.coder = coder
        self.catalog.record_audit(
            request_id, "schema_init", "ok",
            f"dataset {name!r} with {len(specs)} dims",
            {"dimensions": [d.to_dict() for d in specs],
             "total_interleaved_bits": coder.total_bits,
             "code_storage": "uint64" if coder.fits_uint64 else "fixed_binary"},
        )
        log.info("schema initialized", extra={"step": "schema_init", "version": 1,
                                              "detail": {"ndim": len(specs),
                                                         "total_bits": coder.total_bits}})
        return self.schema_info()

    def schema_info(self) -> dict:
        row = self.catalog.require_dataset()
        assert self.coder is not None
        return {
            "name": row["name"],
            "dimensions": [d.to_dict() for d in self._dim_specs],
            "coder_version": row["coder_version"],
            "chunk_format_version": row["chunk_format_version"],
            "chunk_size": row["chunk_size"],
            "total_interleaved_bits": self.coder.total_bits,
            "code_storage": "uint64" if self.coder.fits_uint64 else "fixed_binary",
            "next_row_id": row["next_row_id"],
        }

    def _require_ready(self) -> None:
        if self.coder is None:
            raise NotInitializedError("no dataset initialized")

    def _chunk_path(self, chunk_id: str) -> str:
        return os.path.join(self.root, CHUNKS_SUBDIR, f"{chunk_id}.arrow")

    # -- ingest -----------------------------------------------------------
    def ingest(self, rows: list[dict], request_id: str) -> dict:
        self._require_ready()
        coder = self.coder
        assert coder is not None

        validated: list[tuple[int, list[int]]] = []
        invalid: list[dict] = []
        for i, row in enumerate(rows):
            try:
                coords: list[int] = []
                for d in self._dim_specs:
                    if d.name not in row:
                        raise CoordinateError(f"missing dimension {d.name!r}")
                    value = row[d.name]
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise CoordinateError(
                            f"dimension {d.name!r}: coordinate must be int, got "
                            f"{type(value).__name__}"
                        )
                    d.to_unsigned(value)  # validates range/sign
                    coords.append(value)
                validated.append((-1, coords))
            except (CoordinateError, OutOfDomainError, ValueError) as exc:
                invalid.append({"row_index": i, "reason": str(exc)})

        if not validated:
            self.catalog.record_audit(
                request_id, "ingest", "rejected",
                f"{len(invalid)} invalid rows, 0 accepted",
                {"attempted": len(rows), "accepted": 0, "invalid": invalid[:20]},
            )
            log.warning("ingest rejected", extra={"step": "ingest_validate",
                                                  "detail": {"invalid": len(invalid)}})
            raise CoordinateError(f"no valid rows to ingest; {len(invalid)} invalid")

        ids = self.catalog.next_row_ids(len(validated))
        validated = [(rid, coords) for rid, (_, coords) in zip(ids, validated)]

        tables = self._build_chunk_tables(validated)
        records: list[ChunkRecord] = []
        for table, code_lo, code_hi in tables:
            chunk_id = uuid.uuid4().hex
            path = self._chunk_path(chunk_id)
            write_chunk(path, table, compression=self.cfg.arrow_compression)
            records.append(ChunkRecord(
                chunk_id=chunk_id, path=path, row_count=table.num_rows,
                code_min=code_lo, code_max=code_hi,
                format_version=CHUNK_FORMAT_VERSION, created_by=request_id,
            ))
        self.catalog.add_chunks(records)
        self.catalog.record_audit(
            request_id, "ingest", "ok",
            f"accepted {len(validated)} rows into {len(records)} chunks",
            {"attempted": len(rows), "accepted": len(validated),
             "invalid": invalid[:20], "chunk_ids": [r.chunk_id for r in records]},
        )
        log.info("ingest complete", extra={
            "step": "ingest_write",
            "location": f"{len(records)} chunks under {CHUNKS_SUBDIR}/",
            "detail": {"accepted": len(validated), "invalid": len(invalid),
                       "row_id_range": [validated[0][0], validated[-1][0]]},
        })
        return {
            "accepted": len(validated),
            "invalid_count": len(invalid),
            "invalid": invalid,
            "chunks": [r.chunk_id for r in records],
            "row_id_range": [validated[0][0], validated[-1][0]],
        }

    def _build_chunk_tables(
        self, rows: list[tuple[int, list[int]]]
    ) -> list[tuple[pa.Table, int, int]]:
        """One globally code-sorted table, cut into fixed-row chunk boundaries.

        Splitting one sorted table (rather than per-batch tables) keeps newly
        ingested data clustered as well as possible until compaction.
        """
        coder = self.coder
        assert coder is not None
        table = build_table(coder, rows)
        codes = code_column_as_int(table, coder)
        out = []
        size = self.cfg.chunk_size
        for start in range(0, table.num_rows, size):
            part = table.slice(start, min(size, table.num_rows - start))
            out.append((part, codes[start], codes[start + part.num_rows - 1]))
        return out

    # -- query ------------------------------------------------------------
    def query(
        self,
        box_edges: list[dict],
        request_id: str,
        budget: int | None = None,
    ) -> QueryOutcome:
        self._require_ready()
        coder = self.coder
        assert coder is not None
        budget = self.cfg.default_interval_budget if budget is None else budget
        if not isinstance(budget, int) or isinstance(budget, bool):
            raise BudgetError("interval_budget must be an integer")
        if not 1 <= budget <= self.cfg.max_interval_budget:
            raise BudgetError(
                f"interval_budget must be in 1..{self.cfg.max_interval_budget}"
            )

        box_raw, box_unsigned, steps = self._parse_box(box_edges)
        dec = decompose_box(coder, box_unsigned, budget=budget)
        steps.append(
            f"decomposed box into {len(dec.intervals)} interval(s) "
            f"(budget={budget}, exhausted={dec.budget_exhausted})"
        )
        ivs = [(iv.lo, iv.hi, iv.exact) for iv in dec.intervals]

        outcome_rows: list[dict] = []
        candidate_count = 0
        chunks_read = 0
        chunks_skipped = 0
        bytes_read = 0
        dim_names = [d.name for d in self._dim_specs]

        for rec in self.catalog.list_chunks():
            if not any(not (hi < rec.code_min or lo > rec.code_max)
                       for lo, hi, _ in ivs):
                chunks_skipped += 1
                continue
            chunks_read += 1
            bytes_read += os.path.getsize(rec.path)
            table = read_chunk(rec.path)
            codes = code_column_as_int(table, coder)
            selected = bytearray(table.num_rows)
            for lo, hi, _ in ivs:
                a = bisect_left(codes, lo)
                b = bisect_right(codes, hi)
                for j in range(a, b):
                    selected[j] = 1
            candidate_count += sum(selected)

            row_ids = table.column(ROW_ID_COLUMN).to_pylist()
            cols = [table.column(n).to_pylist() for n in dim_names]
            for j in range(table.num_rows):
                if not selected[j]:
                    continue
                coords = [col[j] for col in cols]
                ucoords = [d.to_unsigned(v)
                           for d, v in zip(self._dim_specs, coords)]
                if point_in_box_unsigned(ucoords, box_unsigned):
                    outcome_rows.append({
                        ROW_ID_COLUMN: int(row_ids[j]),
                        **{n: int(v) for n, v in zip(dim_names, coords)},
                    })

        outcome_rows.sort(key=lambda r: r[ROW_ID_COLUMN])
        result_count = len(outcome_rows)
        inflation = (
            candidate_count - result_count if candidate_count >= result_count else 0
        )
        steps.append(
            f"scanned {chunks_read} chunk(s) ({chunks_skipped} skipped by code "
            f"min/max), {candidate_count} candidate rows, {result_count} after "
            f"exact residual filter"
        )
        uncertainties: list[str] = []
        if dec.budget_exhausted:
            uncertainties.append(
                "interval budget exhausted before the box was fully decomposed; "
                "the full code domain was used conservatively (no misses, but "
                "candidate inflation is expected)"
            )

        stats = {
            "intervals": len(dec.intervals),
            "intervals_exact": len(dec.intervals) - dec.overapproximate_intervals,
            "intervals_inexact": dec.overapproximate_intervals,
            "interval_budget": budget,
            "budget_exhausted": dec.budget_exhausted,
            "intervals_merged_away": dec.merged_away,
            "nodes_emitted": dec.nodes_emitted,
            "chunks_total": chunks_read + chunks_skipped,
            "chunks_read": chunks_read,
            "chunks_skipped": chunks_skipped,
            "bytes_read": bytes_read,
            "candidate_rows": candidate_count,
            "result_rows": result_count,
            "false_positive_rows": inflation,
            "candidate_inflation_ratio": (
                round(candidate_count / result_count, 4) if result_count else None
            ),
        }
        result = QueryOutcome(
            request_id=request_id, rows=outcome_rows,
            box={"raw": box_raw, "unsigned": [list(e) for e in box_unsigned]},
            budget=budget, budget_exhausted=dec.budget_exhausted,
            intervals=[{"lo": lo, "hi": hi, "exact": ex} for lo, hi, ex in ivs],
            stats=stats, steps=steps, uncertainties=uncertainties,
        )
        self.catalog.record_audit(
            request_id, "query", "ok",
            f"{result_count} rows; {chunks_read}/{stats['chunks_total']} chunks read",
            {"stats": stats, "intervals": result.intervals,
             "uncertainties": uncertainties, "steps": steps},
        )
        log.info("query complete", extra={"step": "query_execute",
                                          "location": f"{chunks_read} chunks read",
                                          "detail": stats})
        return result

    def _parse_box(
        self, box_edges: list[dict]
    ) -> tuple[list[dict], list[tuple[int, int]], list[str]]:
        if not isinstance(box_edges, list) or len(box_edges) != len(self._dim_specs):
            raise QueryValidationError(
                f"box must list one edge per dimension "
                f"({len(self._dim_specs)} expected)"
            )
        steps: list[str] = []
        raw_edges: list[dict] = []
        unsigned: list[tuple[int, int]] = []
        for spec, edge in zip(self._dim_specs, box_edges):
            name = edge.get("dimension")
            if name != spec.name:
                raise QueryValidationError(
                    f"box edge dimension {name!r} does not match schema "
                    f"position {spec.name!r}"
                )
            try:
                lo = int(edge["lo"])
                hi = int(edge["hi"])
            except (KeyError, TypeError, ValueError) as exc:
                raise QueryValidationError(
                    f"edge {name!r}: lo/hi must be integers"
                ) from exc
            if lo > hi:
                raise QueryValidationError(f"edge {name!r}: lo {lo} > hi {hi}")
            try:
                ulo = spec.to_unsigned(lo)
                uhi = spec.to_unsigned(hi)
            except OutOfDomainError as exc:
                raise QueryValidationError(str(exc)) from exc
            raw_edges.append({"dimension": name, "lo": lo, "hi": hi})
            unsigned.append((ulo, uhi))
            steps.append(
                f"edge {name}: signed [{lo}, {hi}] -> unsigned [{ulo}, {uhi}]"
            )
        return raw_edges, unsigned, steps

    # -- full scan reference ---------------------------------------------
    def full_scan(self, box_edges: list[dict], request_id: str) -> dict:
        """Reference implementation: read every chunk, filter raw coordinates.

        No index, no intervals, no coder-based pruning — the ground truth used
        by the verification endpoint and by tests to prove zero missing rows.
        """
        self._require_ready()
        box_raw, box_unsigned, _ = self._parse_box(box_edges)
        dim_names = [d.name for d in self._dim_specs]
        rows: list[dict] = []
        chunks_read = 0
        bytes_read = 0
        for rec in self.catalog.list_chunks():
            chunks_read += 1
            bytes_read += os.path.getsize(rec.path)
            table = read_chunk(rec.path)
            row_ids = table.column(ROW_ID_COLUMN).to_pylist()
            cols = [table.column(n).to_pylist() for n in dim_names]
            for j in range(table.num_rows):
                coords = [col[j] for col in cols]
                ucoords = [d.to_unsigned(v)
                           for d, v in zip(self._dim_specs, coords)]
                if point_in_box_unsigned(ucoords, box_unsigned):
                    rows.append({
                        ROW_ID_COLUMN: int(row_ids[j]),
                        **{n: int(v) for n, v in zip(dim_names, coords)},
                    })
        rows.sort(key=lambda r: r[ROW_ID_COLUMN])
        stats = {"chunks_read": chunks_read, "chunks_skipped": 0,
                 "bytes_read": bytes_read, "result_rows": len(rows)}
        self.catalog.record_audit(
            request_id, "full_scan", "ok",
            f"{len(rows)} rows across all {chunks_read} chunks",
            {"stats": stats, "box": box_raw},
        )
        log.info("full scan complete", extra={"step": "full_scan",
                                              "location": "all chunks",
                                              "detail": stats})
        return {"rows": rows, "stats": stats, "box": {"raw": box_raw}}

    # -- compaction / rewrite --------------------------------------------
    def compact(self, request_id: str) -> dict:
        """Rewrite all rows into fresh, full-size, code-sorted chunks.

        Row identities are copied byte-for-byte from the ``row_id`` column;
        the operation never reassigns ids.  Metadata swaps atomically; old
        files are unlinked only after commit.
        """
        self._require_ready()
        coder = self.coder
        assert coder is not None
        old = self.catalog.list_chunks()
        if not old:
            raise NotInitializedError("store contains no chunks to compact")

        merged: list[tuple[int, list[int]]] = []
        dim_names = [d.name for d in self._dim_specs]
        for rec in old:
            table = read_chunk(rec.path)
            ids = table.column(ROW_ID_COLUMN).to_pylist()
            cols = [table.column(n).to_pylist() for n in dim_names]
            for j in range(table.num_rows):
                merged.append((int(ids[j]), [int(col[j]) for col in cols]))

        before_rows = len(merged)
        before_chunks = len(old)
        tables = self._build_chunk_tables(merged)
        new_records: list[ChunkRecord] = []
        for table, code_lo, code_hi in tables:
            chunk_id = uuid.uuid4().hex
            path = self._chunk_path(chunk_id)
            write_chunk(path, table, compression=self.cfg.arrow_compression)
            new_records.append(ChunkRecord(
                chunk_id=chunk_id, path=path, row_count=table.num_rows,
                code_min=code_lo, code_max=code_hi,
                format_version=CHUNK_FORMAT_VERSION,
                created_by=f"compact:{request_id}",
            ))

        retired = self.catalog.replace_chunks(
            [r.chunk_id for r in old], new_records
        )
        for _cid, path in retired:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

        after_rows = sum(r.row_count for r in new_records)
        stats = {
            "chunks_before": before_chunks,
            "chunks_after": len(new_records),
            "rows_before": before_rows,
            "rows_after": after_rows,
            "row_ids_preserved": before_rows == after_rows,
        }
        self.catalog.record_audit(
            request_id, "compact", "ok",
            f"{before_chunks} chunks -> {len(new_records)}, {before_rows} rows",
            {"stats": stats,
             "retired": [cid for cid, _ in retired],
             "new": [r.chunk_id for r in new_records]},
        )
        log.info("compaction complete", extra={"step": "compact_rewrite",
                                               "version": CHUNK_FORMAT_VERSION,
                                               "detail": stats})
        if before_rows != after_rows:  # defensive; metadata tx makes it impossible
            log.error("row count changed across compaction",
                      extra={"step": "compact_verify", "uncertain": True,
                             "detail": stats})
        return {"stats": stats,
                "chunks": [r.chunk_id for r in new_records]}

    # -- introspection ----------------------------------------------------
    def chunk_descriptors(self) -> list[dict]:
        out = []
        for rec in self.catalog.list_chunks():
            md = chunk_schema_metadata(rec.path)
            out.append({
                "chunk_id": rec.chunk_id,
                "path": os.path.relpath(rec.path, self.root),
                "row_count": rec.row_count,
                "code_min": rec.code_min,
                "code_max": rec.code_max,
                "created_by": rec.created_by,
                "code_storage": md.get(b"code_storage", b"?").decode(),
            })
        return out
