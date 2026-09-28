"""Execution kernel: interval decomposition, chunk pruning, exact filtering.

Query pipeline (every stage is reported in ``steps`` for explainability):

    1. validate raw box on signed/unsigned coordinate space;
    2. map box edges to fixed-width unsigned edges per dimension;
    3. decompose the unsigned box into Morton intervals (budgeted, exact/
       conservative labels preserved);
    4. prune chunks whose global [min_code, max_code] misses every interval;
    5. per surviving chunk: read the columnar file (bytes counted from
       stat()), push the interval disjunction down as an Arrow uint64 pair
       predicate on (__mc_hi,__mc_lo), obtaining candidate rows;
    6. exact residual filter on the ORIGINAL integer coordinate columns with
       numpy - conservative intervals can only widen the candidate set, never
       narrow it, and this step restores exact semantics;
    7. return rows with stable __row_id, plus candidate-bloat / chunk-I/O stats.

An unreadable chunk is not dropped silently: it is listed under
``uncertainties`` (category ``chunk_unreadable``) and flagged in the response
status as ``degraded`` rather than a false "complete".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from .catalog import Catalog, ChunkRecord
from .chunkstore import (
    MC_HI_COL,
    MC_LO_COL,
    ROW_ID_COL,
    ChunkUnreadable,
    byte_size,
    read_table,
)
from .encoding import (
    Interval,
    SchemaSpec,
    decompose_box,
    split128,
    to_unsigned,
)


@dataclass
class QueryOutcome:
    rows: list[dict[str, Any]]
    steps: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    uncertainties: list[dict[str, Any]] = field(default_factory=list)
    status: str = "complete"


def _check_raw_box(schema: SchemaSpec, raw_lo: list[int], raw_hi: list[int]) -> None:
    if len(raw_lo) != len(schema.dims) or len(raw_hi) != len(schema.dims):
        from .errors import InvalidBox

        raise InvalidBox(
            "box must give one [lo, hi] pair per dimension",
            expected_dims=[d.name for d in schema.dims],
        )
    for dim, a, b in zip(schema.dims, raw_lo, raw_hi):
        if not isinstance(a, int) or not isinstance(b, int) or isinstance(a, bool) or isinstance(b, bool):
            from .errors import InvalidBox

            raise InvalidBox(f"bounds for {dim.name!r} must be integers")
        if a > b:
            from .errors import InvalidBox

            raise InvalidBox(f"box edge lo>hi on {dim.name!r}", lo=a, hi=b)


def raw_box_to_unsigned(
    schema: SchemaSpec, raw_lo: list[int], raw_hi: list[int]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Map a raw inclusive box to its unsigned image.

    Zig-zag is monotone (0,-1,1,-2,... in unsigned order), and a contiguous
    raw range maps to one contiguous unsigned range - either
    ``[zz(lo), zz(hi)]`` (lo >= 0), ``[zz(hi), zz(lo)]`` (hi < 0), or
    ``[0, max(zz(lo), zz(hi))]`` for a range straddling zero. Unsigned
    dimensions map directly.
    """
    _check_raw_box(schema, raw_lo, raw_hi)
    from .errors import InvalidBox

    ulo: list[int] = []
    uhi: list[int] = []
    for dim, a, b in zip(schema.dims, raw_lo, raw_hi):
        try:
            ua, ub = to_unsigned(a, dim), to_unsigned(b, dim)
        except ValueError as exc:
            raise InvalidBox(str(exc), dim=dim.name, lo=a, hi=b) from exc
        if dim.signed and a < 0 and b > 0:
            ulo.append(0)
            uhi.append(max(ua, ub))
        else:
            ulo.append(min(ua, ub))
            uhi.append(max(ua, ub))
    return tuple(ulo), tuple(uhi)


def _scalar(value: int) -> pa.Scalar:
    return pa.scalar(value, type=pa.uint64())


def interval_mask_128(table: pa.Table, intervals: tuple[Interval, ...]) -> pa.ChunkedArray:
    """Evaluate the interval disjunction as a flat mask.

    We deliberately do NOT build one deeply nested ``|`` expression and hand
    it to ``Table.filter``: Arrow 18's expression Canonicalize recurses over
    nested OR nodes and overflows its native stack once dozens of 128-bit
    intervals are ORed (observed SIGSEGV in ``ModifyExpression``). Instead we
    evaluate each interval - itself a shallow DNF predicate - directly with
    the compute kernels and fold the resulting masks with ``or_``.
    """
    hi = table.column(MC_HI_COL)
    lo = table.column(MC_LO_COL)
    mask = None
    for iv in intervals:
        iv_hi_a, iv_lo_a = split128(iv.lo)
        iv_hi_b, iv_lo_b = split128(iv.hi)
        if iv_hi_a == iv_hi_b:
            piece = pc.and_(
                pc.equal(hi, _scalar(iv_hi_a)),
                pc.and_(
                    pc.greater_equal(lo, _scalar(iv_lo_a)),
                    pc.less_equal(lo, _scalar(iv_lo_b)),
                ),
            )
        else:
            low_tail = pc.and_(
                pc.equal(hi, _scalar(iv_hi_a)),
                pc.greater_equal(lo, _scalar(iv_lo_a)),
            )
            mid = pc.and_(
                pc.greater(hi, _scalar(iv_hi_a)),
                pc.less(hi, _scalar(iv_hi_b)),
            )
            high_tail = pc.and_(
                pc.equal(hi, _scalar(iv_hi_b)),
                pc.less_equal(lo, _scalar(iv_lo_b)),
            )
            piece = pc.or_(low_tail, pc.or_(mid, high_tail))
        mask = piece if mask is None else pc.or_(mask, piece)
    if mask is None:
        return _all_false_mask(hi)
    return mask


def _all_false_mask(reference_col: pa.ChunkedArray) -> pa.ChunkedArray:
    return pa.chunked_array(
        [pa.array([False] * len(c), type=pa.bool_()) for c in reference_col.chunks],
        type=pa.bool_(),
    )


def chunk_might_overlap(rec: ChunkRecord, intervals: tuple[Interval, ...]) -> bool:
    return any(rec.min_code <= iv.hi and iv.lo <= rec.max_code for iv in intervals)


def full_scan_reference(
    schema: SchemaSpec, rows: list[tuple[int, ...]], raw_lo: list[int], raw_hi: list[int]
) -> list[int]:
    """Naive O(n*d) reference implementation over raw coordinates."""
    out = []
    for idx, row in enumerate(rows):
        if all(a <= v <= b for v, a, b in zip(row, raw_lo, raw_hi)):
            out.append(idx)
    return out


class Kernel:
    def __init__(self, catalog: Catalog, data_dir: str | Path) -> None:
        self.catalog = catalog
        self.data_dir = Path(data_dir)

    # ----------------------------------------------------------------- query
    def query(
        self,
        schema_name: str,
        raw_lo: list[int],
        raw_hi: list[int],
        max_intervals: int,
        limit: int | None = None,
    ) -> QueryOutcome:
        outcome = QueryOutcome(rows=[])
        schema = self.catalog.get_schema(schema_name)

        ulo, uhi = raw_box_to_unsigned(schema, raw_lo, raw_hi)
        outcome.steps.append({
            "step": "box_mapped",
            "raw_lo": raw_lo,
            "raw_hi": raw_hi,
            "unsigned_lo": list(ulo),
            "unsigned_hi": list(uhi),
        })

        decomp = decompose_box(schema, ulo, uhi, max_intervals)
        intervals = decomp.intervals
        outcome.steps.append({
            "step": "box_decomposed",
            "num_intervals": len(intervals),
            "exact_intervals": decomp.exact_intervals,
            "conservative_intervals": decomp.conservative_intervals,
            "budget": max_intervals,
            "budget_exhausted": decomp.budget_exhausted,
            "cells_split": decomp.cells_split,
            "levels_visited": decomp.levels_visited,
            "sample_intervals": [
                {"lo": str(iv.lo), "hi": str(iv.hi), "exact": iv.exact}
                for iv in intervals[:8]
            ],
        })

        all_chunks = self.catalog.list_chunks(schema_name)
        survivors = [c for c in all_chunks if chunk_might_overlap(c, intervals)]
        skipped = len(all_chunks) - len(survivors)
        outcome.steps.append({
            "step": "chunks_pruned",
            "chunks_total": len(all_chunks),
            "chunks_selected": len(survivors),
            "chunks_skipped": skipped,
            "selected_chunk_ids": [c.chunk_id for c in survivors],
        })

        total_rows = 0
        total_candidates = 0
        total_bytes = 0
        matched_rows: list[dict[str, Any]] = []

        lo_np = np.array(raw_lo, dtype=np.int64)
        hi_np = np.array(raw_hi, dtype=np.int64)

        for rec in survivors:
            total_rows += rec.num_rows
            total_bytes += byte_size(rec.path)
            try:
                table = read_table(rec.path, rec.chunk_id)
            except ChunkUnreadable as exc:
                outcome.uncertainties.append({
                    "category": "chunk_unreadable",
                    "chunk_id": rec.chunk_id,
                    "path": str(exc.path),
                    "reason": exc.reason,
                    "num_rows_skipped": rec.num_rows,
                })
                outcome.status = "degraded"
                continue

            if intervals:
                mask = interval_mask_128(table, intervals)
                candidate = table.filter(mask)
            else:
                candidate = table.slice(0, 0)
            cand_count = candidate.num_rows
            total_candidates += cand_count

            dim_cols = [np.asarray(candidate.column(d.name).to_numpy(zero_copy_only=False))
                        for d in schema.dims]
            if dim_cols:
                keep = np.ones(cand_count, dtype=bool)
                for j in range(len(schema.dims)):
                    keep &= (dim_cols[j] >= lo_np[j]) & (dim_cols[j] <= hi_np[j])
            else:
                keep = np.ones(cand_count, dtype=bool)

            rids = candidate.column(ROW_ID_COL).to_numpy(zero_copy_only=False)
            keep_idx = np.nonzero(keep)[0]
            for i in keep_idx:
                ii = int(i)
                row = {ROW_ID_COL: int(rids[ii])}
                for d in schema.dims:
                    row[d.name] = int(candidate.column(d.name)[ii].as_py())
                matched_rows.append(row)

            outcome.steps.append({
                "step": "chunk_scanned",
                "chunk_id": rec.chunk_id,
                "path": rec.path,
                "rows_in_chunk": rec.num_rows,
                "code_candidates": cand_count,
                "exact_matches": int(keep.sum()),
                "byte_size": rec.byte_size,
            })

        matched_rows.sort(key=lambda r: r[ROW_ID_COL])
        total_matched = len(matched_rows)
        if limit is not None and limit >= 0:
            matched_rows = matched_rows[:limit]

        # Bloat = extra candidates beyond true matches per returned match.
        # Undefined (null) when there are no true matches at all - a raw
        # candidate count there would masquerade as an infinite ratio.
        bloat = ((total_candidates - total_matched) / total_matched) if total_matched else None
        outcome.rows = matched_rows
        outcome.stats = {
            "rows_in_selected_chunks": total_rows,
            "code_candidates": total_candidates,
            "exact_matches": total_matched,
            "returned_rows": len(matched_rows),
            "candidate_bloat_ratio": (round(bloat, 6) if bloat is not None else None),
            "chunks_total": len(all_chunks),
            "chunks_selected": len(survivors),
            "chunks_skipped": skipped,
            "chunk_bytes_read": total_bytes,
            "intervals": len(intervals),
            "exact_intervals": decomp.exact_intervals,
            "conservative_intervals": decomp.conservative_intervals,
            "budget_exhausted": decomp.budget_exhausted,
        }
        if decomp.budget_exhausted:
            outcome.uncertainties.append({
                "category": "budget_exhausted",
                "message": (
                    "interval decomposition budget exhausted; conservative intervals "
                    "widen candidates but exact residual filter guarantees no missing rows"
                ),
                "budget": max_intervals,
                "conservative_intervals": decomp.conservative_intervals,
            })
        return outcome
