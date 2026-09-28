"""End-to-end store tests: chunk I/O, zero-miss queries, stable rewrites."""

from __future__ import annotations

import json
import random

import pytest

from zcluster.errors import (
    AlreadyInitializedError,
    CoordinateError,
    NotInitializedError,
    QueryValidationError,
)
from zcluster.format.chunks import read_chunk, write_chunk
from zcluster.kernel.coder import DimSpec


DIMS = [DimSpec("x", 5, signed=True), DimSpec("y", 5, signed=True)]


def _rows(points):
    return [{"x": p[0], "y": p[1]} for p in points]


def _edges(lo, hi):
    return [{"dimension": "x", "lo": lo[0], "hi": hi[0]},
            {"dimension": "y", "lo": lo[1], "hi": hi[1]}]


def test_operations_before_init_are_rejected(make_store):
    with make_store() as store:
        with pytest.raises(NotInitializedError):
            store.ingest([{"x": 1, "y": 1}], "rid-x")
        with pytest.raises(NotInitializedError):
            store.query(_edges((0, 0), (1, 1)), "rid-y")


def test_double_init_rejected(make_store):
    with make_store() as store:
        store.initialize("d", [d.to_dict() for d in DIMS], "r1")
        with pytest.raises(AlreadyInitializedError):
            store.initialize("d2", [d.to_dict() for d in DIMS], "r2")


def test_invalid_coordinates_rejected_with_category(make_store):
    with make_store() as store:
        store.initialize("d", [d.to_dict() for d in DIMS], "r1")
        with pytest.raises(CoordinateError):
            store.ingest([{"x": 100, "y": 0}], "r2")  # 5-bit signed max is 15
        with pytest.raises(CoordinateError):
            store.ingest([{"x": 0, "y": "1"}], "r3")
        with pytest.raises(CoordinateError):
            store.ingest([{"x": 0}], "r4")
        # nothing got through
        assert store.catalog.list_chunks() == []
        assert store.catalog.dataset_row()["next_row_id"] == 0


def test_ingest_chunks_are_sorted_and_query_matches_full_scan(make_store):
    rng = random.Random(7)
    points = [(rng.randint(-16, 15), rng.randint(-16, 15)) for _ in range(50)]
    with make_store() as store:
        store.initialize("d", [d.to_dict() for d in DIMS], "init")
        ing = store.ingest(_rows(points), "ing")
        assert ing["accepted"] == 50 and ing["invalid_count"] == 0
        chunks = store.catalog.list_chunks()
        # chunk_size=4 => ceil(50/4) = 13 chunks, each sorted within
        assert len(chunks) == 13
        for rec in chunks:
            table = read_chunk(rec.path)
            codes = [int(c) for c in table.column("code").to_pylist()]
            assert codes == sorted(codes)
            assert rec.code_min == codes[0] and rec.code_max == codes[-1]

        box = _edges((-5, -5), (5, 5))
        q = store.query(box, "q", budget=256)
        fs = store.full_scan(box, "fs")
        assert [r["row_id"] for r in q.rows] == [r["row_id"] for r in fs["rows"]]
        expected = {i for i, p in enumerate(points) if -5 <= p[0] <= 5 and -5 <= p[1] <= 5}
        assert {r["row_id"] for r in q.rows} == expected


def test_boundary_and_negative_boxes(make_store):
    points = [(-16, -16), (-16, 15), (15, -16), (15, 15), (-1, 0), (0, -1), (0, 0)]
    with make_store() as store:
        store.initialize("d", [d.to_dict() for d in DIMS], "init")
        store.ingest(_rows(points), "ing")
        # full domain catches all 7
        q = store.query(_edges((-16, -16), (15, 15)), "q1")
        assert q.stats["result_rows"] == 7
        # negative corner
        q = store.query(_edges((-16, -16), (-16, -16)), "q2")
        assert {r["row_id"] for r in q.rows} == {0}
        # thin sign-boundary slab x in [-1,0], full y
        q = store.query(_edges((-1, -16), (0, 15)), "q3")
        assert {r["row_id"] for r in q.rows} == {4, 5, 6}


def test_budget1_inflates_but_never_misses(make_store):
    rng = random.Random(11)
    points = [(rng.randint(-16, 15), rng.randint(-16, 15)) for _ in range(200)]
    box = _edges((-3, -3), (3, 3))
    with make_store() as store:
        store.initialize("d", [d.to_dict() for d in DIMS], "init")
        store.ingest(_rows(points), "ing")
        tight = store.query(box, "q-tight", budget=4096)
        loose = store.query(box, "q-loose", budget=1)
        assert loose.budget_exhausted is True
        assert loose.uncertainties  # uncertainty surfaced separately
        assert {r["row_id"] for r in loose.rows} == \
            {r["row_id"] for r in tight.rows}
        # every chunk read under the escape; inflation strictly higher
        assert loose.stats["chunks_read"] == loose.stats["chunks_total"]
        assert loose.stats["candidate_rows"] >= tight.stats["candidate_rows"]
        assert loose.stats["false_positive_rows"] >= 0


def test_chunk_pruning_reduces_reads(make_store):
    # clustered corners: low codes in one chunk region, high in another
    points = [(-16, -16), (-15, -15), (-16, -15), (-15, -16),
              (15, 15), (14, 14), (15, 14), (14, 15)]
    with make_store() as store:
        store.initialize("d", [d.to_dict() for d in DIMS], "init")
        store.ingest(_rows(points), "ing")
        q = store.query(_edges((-16, -16), (-14, -14)), "q", budget=64)
        assert q.stats["chunks_skipped"] >= 1
        assert q.stats["chunks_read"] < q.stats["chunks_total"]
        assert q.stats["result_rows"] == 4
        assert q.stats["bytes_read"] > 0


def test_compaction_preserves_row_identities_and_reclusters(make_store):
    rng = random.Random(13)
    with make_store() as store:
        store.initialize("d", [d.to_dict() for d in DIMS], "init")
        all_points = [(rng.randint(-16, 15), rng.randint(-16, 15)) for _ in range(40)]
        for i in range(0, 40, 2):  # chunks of 2 into chunk_size=4 => many partial
            store.ingest(_rows(all_points[i:i + 2]), f"ing-{i}")
        before = store.catalog.list_chunks()
        assert len(before) > 10
        snap = {}
        for rec in before:
            t = read_chunk(rec.path)
            for rid, x, y in zip(t.column("row_id").to_pylist(),
                                 t.column("x").to_pylist(),
                                 t.column("y").to_pylist()):
                snap[int(rid)] = (int(x), int(y))

        comp = store.compact("compact-1")
        assert comp["stats"]["rows_before"] == 40
        assert comp["stats"]["rows_after"] == 40
        assert comp["stats"]["row_ids_preserved"] is True
        after = store.catalog.list_chunks()
        assert len(after) == 10  # 40 rows / chunk_size 4
        after_snap = {}
        for rec in after:
            t = read_chunk(rec.path)
            for rid, x, y in zip(t.column("row_id").to_pylist(),
                                 t.column("x").to_pylist(),
                                 t.column("y").to_pylist()):
                after_snap[int(rid)] = (int(x), int(y))
        assert snap == after_snap
        # row id sequence still monotonic after rewrite + new ingest
        more = store.ingest(_rows([(1, 1), (2, 2)]), "ing-after")
        assert more["row_id_range"] == [40, 41]
        q = store.query(_edges((-16, -16), (15, 15)), "q-after")
        assert q.stats["result_rows"] == 42


def test_query_validation_error_categories(make_store):
    with make_store() as store:
        store.initialize("d", [d.to_dict() for d in DIMS], "init")
        store.ingest(_rows([(0, 0)]), "ing")
        with pytest.raises(QueryValidationError):
            store.query([{"dimension": "x", "lo": 0, "hi": 1}], "q")  # missing edge
        with pytest.raises(QueryValidationError):
            store.query([{"dimension": "y", "lo": 0, "hi": 1},
                         {"dimension": "x", "lo": 0, "hi": 1}], "q2")  # wrong order
        with pytest.raises(QueryValidationError):
            store.query(_edges((5, 0), (1, 1)), "q3")  # lo > hi
        with pytest.raises(QueryValidationError):
            store.query(_edges((-100, 0), (1, 1)), "q4")  # out of domain


def test_audit_correlates_request_ids(make_store):
    with make_store() as store:
        store.initialize("d", [d.to_dict() for d in DIMS], "rid-init")
        store.ingest(_rows([(0, 0), (1, 1)]), "rid-ing")
        store.query(_edges((0, 0), (1, 1)), "rid-q")
        rec = store.catalog.get_audit("rid-q")
        assert rec["request_id"] == "rid-q"
        assert rec["kind"] == "query"
        assert rec["status"] == "ok"
        assert rec["detail"]["stats"]["result_rows"] == 2


def test_wider_than_64bit_codes_use_binary_storage(wide_config):
    # 4 dims x 24 bits = 96 interleaved bits
    dims = [DimSpec(n, 24, signed=True) for n in ("a", "b", "c", "d")]
    from zcluster.core.store import Store
    with Store(wide_config) as store:
        store.initialize("wide", [d.to_dict() for d in dims], "init")
        pts = [{"a": 100000, "b": -100000, "c": 0, "d": 8388607},
               {"a": -1, "b": -1, "c": -1, "d": -1},
               {"a": 0, "b": 0, "c": 0, "d": 0}]
        store.ingest(pts, "ing")
        t = read_chunk(store.catalog.list_chunks()[0].path)
        md = {k.decode(): v.decode() for k, v in (t.schema.metadata or {}).items()}
        assert md["code_storage"] == "fixed_binary"
        assert md["code_byte_width"] == "12"
        edges = [{"dimension": n, "lo": -1, "hi": 1} for n in ("a", "b", "c")]
        edges.append({"dimension": "d", "lo": -1, "hi": 1})
        q = store.query(edges, "q", budget=64)
        fs = store.full_scan(edges, "fs")
        assert {r["row_id"] for r in q.rows} == {r["row_id"] for r in fs["rows"]}
        assert {r["row_id"] for r in q.rows} == {1, 2}
