"""End-to-end kernel + storage tests against an independent brute force.

The oracle here is a naive nested-loop comparison over the *raw* rows handed
to ingestion; the kernel never participates in building the expected answer.
Assertions cover exact row-id sets, zero missed rows at every budget,
candidate bloat bookkeeping, chunk pruning/byte accounting, stable identity
across rewrites, and the degraded (not silently-complete) unreadable-chunk
path.
"""
from __future__ import annotations

import itertools

import pytest

from zindex.chunkstore import chunk_path
from zindex.encoding import DimSpec, SchemaSpec
from zindex.fixtures import generate_rows
from zindex.ingest import IngestService
from zindex.kernel import Kernel
from zindex.chunkstore import ROW_ID_COL


def brute_force(rows, lo, hi):
    return {
        i for i, row in enumerate(rows)
        if all(a <= v <= b for v, a, b in zip(row, lo, hi))
    }


@pytest.fixture()
def service(tmp_env):
    ing = IngestService(
        tmp_env["catalog"], tmp_env["data_dir"],
        default_capacity=tmp_env["settings"].default_chunk_capacity,
    )
    ker = Kernel(tmp_env["catalog"], tmp_env["data_dir"])
    return ing, ker, tmp_env


def _make(service, name, dims, rows, capacity=500):
    ing, _, env = service
    ing.create_schema(name, dims)
    ing.ingest_rows(name, rows, capacity=capacity)


DIMS_2D_S4 = [
    {"name": "x", "bits": 4, "signed": True},
    {"name": "y", "bits": 4, "signed": True},
]


def test_query_matches_brute_force_on_grid(service):
    ing, ker, _ = service
    spec = SchemaSpec(dims=(DimSpec("x", 3, signed=True), DimSpec("y", 3, signed=True)), name="grid")
    rows = generate_rows(spec, 0, shape="grid")  # 49 points (7x7 centered)
    _make(service, "grid", [
        {"name": "x", "bits": 3, "signed": True},
        {"name": "y", "bits": 3, "signed": True},
    ], rows, capacity=10)
    # exhaustively try every box on the small 4x4 grid (256 boxes)
    pts = sorted(set(rows))
    xs = sorted({r[0] for r in rows})
    ys = sorted({r[1] for r in rows})
    checked = 0
    for x0, x1 in itertools.combinations_with_replacement(xs, 2):
        for y0, y1 in itertools.combinations_with_replacement(ys, 2):
            lo, hi = [x0, y0], [x1, y1]
            truth = brute_force(rows, lo, hi)
            out = ker.query("grid", lo, hi, 32)
            got = {r[ROW_ID_COL] for r in out.rows}
            assert got == truth, (lo, hi, got ^ truth)
            checked += 1
    assert checked == len(list(itertools.combinations_with_replacement(xs, 2))) ** 2
    assert checked == 100


def test_zero_missed_rows_at_every_budget_sparse(service):
    ing, ker, _ = service
    spec = SchemaSpec(dims=tuple(DimSpec(f"d{i}", 5, signed=True) for i in range(4)), name="hd")
    rows = generate_rows(spec, 3000, shape="sparse_hd", seed=42)
    _make(service, "hd", [
        {"name": f"d{i}", "bits": 5, "signed": True} for i in range(4)
    ], rows, capacity=700)
    boxes = [
        ([-16, -16, -16, -16], [-12, -12, -12, -12]),   # thin negative corner
        ([-1, -1, -1, -1], [1, 1, 1, 1]),                # straddles zero
        ([10, 10, 10, 10], [15, 15, 15, 15]),            # positive slab
        ([-16] * 4, [15] * 4),                            # whole universe
        ([0, 0, 0, 0], [0, 0, 0, 0]),                    # single point
        ([-16, -16, -16, -16], [15, -15, 15, -15]),      # two thin dims
    ]
    for lo, hi in boxes:
        truth = brute_force(rows, lo, hi)
        for budget in (1, 4, 16, 256):
            out = ker.query("hd", lo, hi, budget)
            got = {r[ROW_ID_COL] for r in out.rows}
            assert got == truth, (lo, hi, budget, len(got), len(truth))
            assert out.stats["exact_matches"] == len(truth)
            if budget == 1 and truth:
                # tight budgets are allowed to be bloated but flagged
                assert out.stats["code_candidates"] >= len(truth)


def test_candidate_bloat_shrinks_with_budget(service):
    ing, ker, _ = service
    spec = SchemaSpec(dims=(DimSpec("x", 8, signed=True), DimSpec("y", 8, signed=True)), name="b")
    rows = generate_rows(spec, 6000, shape="uniform", seed=9)
    _make(service, "b", [
        {"name": "x", "bits": 8, "signed": True},
        {"name": "y", "bits": 8, "signed": True},
    ], rows, capacity=1500)
    lo, hi = [-30, -30], [30, 30]
    bloats = []
    for budget in (1, 8, 128, 4096):
        out = ker.query("b", lo, hi, budget)
        bloats.append(out.stats["code_candidates"])
        truth = brute_force(rows, lo, hi)
        assert {r[ROW_ID_COL] for r in out.rows} == truth
    assert bloats[0] >= bloats[1] >= bloats[2] >= bloats[3], bloats
    # high budget must be strictly tighter than the root cover
    assert bloats[-1] < bloats[0]


def test_chunk_pruning_and_byte_accounting(service):
    ing, ker, env = service
    spec = SchemaSpec(dims=(DimSpec("x", 8, signed=True), DimSpec("y", 8, signed=True)), name="p")
    # Deterministically corner-concentrated rows, in 4 Morton-sorted blocks.
    n = 4000
    # corner of point k; interleave corners so every ingestion batch spans
    # all four corners (pre-rewrite blocks must overlap in code space)
    rows = []
    for k in range(n):
        corner = k % 4
        row = []
        for j in range(2):
            row.append(127 if (corner >> j) & 1 else -128)
        rows.append(tuple(row))
    _make(service, "p", [
        {"name": "x", "bits": 8, "signed": True},
        {"name": "y", "bits": 8, "signed": True},
    ], rows, capacity=1000)
    box_lo, box_hi = [-128, -128], [-128, -128]
    truth = brute_force(rows, box_lo, box_hi)
    pre = ker.query("p", box_lo, box_hi, 256)
    assert {r[ROW_ID_COL] for r in pre.rows} == truth
    # ingestion-order blocks overlap in code space; a global rewrite makes
    # chunk code ranges disjoint so the corner box can prune chunks
    ing.rewrite_all("p", capacity=1000)
    out = ker.query("p", box_lo, box_hi, 256)
    assert {r[ROW_ID_COL] for r in out.rows} == truth
    assert len(truth) == 1000
    assert out.stats["chunks_total"] == 4
    assert out.stats["chunks_skipped"] == 3
    assert out.stats["chunks_skipped"] > pre.stats["chunks_skipped"]
    assert out.stats["chunks_selected"] == out.stats["chunks_total"] - out.stats["chunks_skipped"]
    # bytes reflect only the files actually opened
    catalog_recs = {c.chunk_id: c for c in env["catalog"].list_chunks("p")}
    expected_bytes = sum(
        catalog_recs[cid].byte_size for cid in
        [s["chunk_id"] for s in out.steps if s.get("step") == "chunk_scanned"]
    )
    assert out.stats["chunk_bytes_read"] == expected_bytes
    assert out.stats["chunk_bytes_read"] > 0


def test_rewrite_preserves_stable_row_identity(service):
    ing, ker, _ = service
    rows = generate_rows(
        SchemaSpec(dims=(DimSpec("x", 5, signed=True), DimSpec("y", 5, signed=True))),
        2500, shape="uniform", seed=11,
    )
    _make(service, "rw", [
        {"name": "x", "bits": 5, "signed": True},
        {"name": "y", "bits": 5, "signed": True},
    ], rows, capacity=900)
    lo, hi = [-8, -8], [9, 9]
    before = {r[ROW_ID_COL]: (r["x"], r["y"]) for r in ker.query("rw", lo, hi, 64).rows}
    rw = ing.rewrite_all("rw", capacity=400)
    assert rw["row_id_preserved"] is True
    assert set(rw["old_chunks"]).isdisjoint(set(c["chunk_id"] for c in rw["new_chunks"]))
    after = {r[ROW_ID_COL]: (r["x"], r["y"]) for r in ker.query("rw", lo, hi, 64).rows}
    assert before == after
    # global identity: every surviving id still maps to its original row
    allrows = {r[ROW_ID_COL]: (r["x"], r["y"]) for r in ker.query("rw", [-16, -16], [15, 15], 64).rows}
    for rid, vals in allrows.items():
        assert tuple(vals) == tuple(rows[rid])


def test_unreadable_chunk_is_degraded_not_complete(service):
    ing, ker, env = service
    rows = generate_rows(
        SchemaSpec(dims=(DimSpec("x", 4, signed=True),)),
        800, shape="uniform", seed=5,
    )
    _make(service, "corrupt", [{"name": "x", "bits": 4, "signed": True}], rows, capacity=200)
    # delete one chunk file out-of-band; a covering query must flag it
    recs = env["catalog"].list_chunks("corrupt")
    target = chunk_path(env["data_dir"], "corrupt", recs[0].chunk_id)
    target.unlink()
    out = ker.query("corrupt", [-8], [7], 8)
    cats = [u["category"] for u in out.uncertainties]
    assert "chunk_unreadable" in cats
    assert out.status == "degraded"
    unc = next(u for u in out.uncertainties if u["category"] == "chunk_unreadable")
    assert unc["chunk_id"] == recs[0].chunk_id
    # rows from the live chunks are still returned
    assert len(out.rows) > 0


def test_empty_box_and_limit(service):
    ing, ker, _ = service
    rows = [[i - 8, i - 8] for i in range(16)]  # diagonal only
    _make(service, "line", DIMS_2D_S4, rows, capacity=50)
    # (0,1) is inside the domain but off the diagonal -> genuinely empty
    out = ker.query("line", [0, 1], [0, 1], 32)
    assert out.rows == []
    assert out.stats["code_candidates"] == 0
    out2 = ker.query("line", [-8, -8], [7, 7], 32, limit=3)
    assert len(out2.rows) == 3
    assert out2.stats["exact_matches"] == 16
