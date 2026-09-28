"""Verification checks: round-trip and zero-miss proofs against references.

Every check is self-contained, uses local synthetic fixtures, compares against
either the independent reference codec (``reference.naive``) or the store's
own full-scan ground truth, and returns a structured verdict with a concrete
failure category — never a bare "it ran" assertion.

Failure categories:
  ROUNDTRIP_MISMATCH   coder/decoder disagree with the independent reference
  HIGH_BITS_TRUNCATED  an encoded code reaches >= 2^expected_bits
  COVERAGE_MISS        a decomposed interval union misses a true box code
  RESIDUAL_MISS        a range query dropped a row that full scan returned
  RESIDUAL_FALSE_ROW   a range query returned a row full scan did not
  ROW_ID_UNSTABLE      row ids changed across a rewrite/compaction
  INVARIANT_MISMATCH   statistics or counts violate their stated bounds
"""

from __future__ import annotations

import random

from ..core.store import Store
from ..kernel.coder import DimSpec, MortonCoder
from ..kernel.decompose import decompose_box
from reference.naive import deinterleave, naive_code_members, spread_interleave

CHECKS_VERSION = 1


def _verdict(name: str, ok: bool, category: str | None, detail: dict) -> dict:
    return {"name": name, "ok": ok, "failure_category": category, "detail": detail}


def check_roundtrip() -> dict:
    """Codec round-trip incl. boundary/negative coordinates vs. reference."""
    dims = [DimSpec("x", 4, signed=True), DimSpec("y", 3, signed=False),
            DimSpec("z", 5, signed=True)]
    coder = MortonCoder(dims)
    cases = [
        [d.raw_min for d in dims],
        [d.raw_max for d in dims],
        [0, 0, 0], [-1, 0, -1], [1, 1, 1],
        [-8, 7, -16], [7, 0, 15], [-8, 0, 0], [0, 0, -16],
    ]
    for values in cases:
        code = coder.encode(values)
        if code >= (1 << coder.total_bits):
            return _verdict("roundtrip", False, "HIGH_BITS_TRUNCATED",
                            {"values": values, "code": code,
                             "max_code": (1 << coder.total_bits) - 1})
        ref_code = spread_interleave(
            [d.to_unsigned(v) for d, v in zip(dims, values)],
            [d.bits for d in dims],
        )
        if code != ref_code:
            return _verdict("roundtrip", False, "ROUNDTRIP_MISMATCH",
                            {"values": values, "kernel_code": code,
                             "reference_code": ref_code})
        if coder.decode(code) != values:
            return _verdict("roundtrip", False, "ROUNDTRIP_MISMATCH",
                            {"values": values, "decoded": coder.decode(code)})
        if deinterleave(code, [d.bits for d in dims]) != \
                [d.to_unsigned(v) for d, v in zip(dims, values)]:
            return _verdict("roundtrip", False, "ROUNDTRIP_MISMATCH",
                            {"values": values, "stage": "reference deinterleave"})
    # High-bit stress: a max coordinate must set the top interleaved bits.
    top = coder.encode([d.raw_max for d in dims])
    if not (top >> (coder.total_bits - coder.ndim)):
        return _verdict("roundtrip", False, "HIGH_BITS_TRUNCATED",
                        {"top_code": top, "total_bits": coder.total_bits})
    return _verdict("roundtrip", True, None,
                    {"cases": len(cases), "total_bits": coder.total_bits})


def check_box_coverage() -> dict:
    """Decomposed union covers every true code (tiny exhaustive domain)."""
    dims = [DimSpec("a", 3, signed=True), DimSpec("b", 2, signed=False)]
    coder = MortonCoder(dims)
    boxes = [
        {  # boundary + negative corner box
            "edges": [{"dimension": "a", "lo": -4, "hi": -3},
                      {"dimension": "b", "lo": 0, "hi": 1}]
        },
        {  # thin (single-row) box crossing the signed origin
            "edges": [{"dimension": "a", "lo": -1, "hi": 0},
                      {"dimension": "b", "lo": 2, "hi": 2}]
        },
        {  # full domain
            "edges": [{"dimension": "a", "lo": -4, "hi": 3},
                      {"dimension": "b", "lo": 0, "hi": 3}]
        },
    ]
    widths = [d.bits for d in dims]
    for budget in (1, 4, 1000):
        for box in boxes:
            edges = box["edges"]
            unsigned = []
            for d, e in zip(dims, edges):
                unsigned.append((d.to_unsigned(e["lo"]),
                                 d.to_unsigned(e["hi"])))
            result = decompose_box(coder, unsigned, budget=budget)
            union = [(iv.lo, iv.hi) for iv in result.intervals]
            members = sorted(set(naive_code_members(widths, unsigned)))
            missing = [c for c in members
                       if not any(lo <= c <= hi for lo, hi in union)]
            if missing:
                return _verdict("box_coverage", False, "COVERAGE_MISS",
                                {"box": edges, "budget": budget,
                                 "missing_codes": missing[:10]})
            # intervals must be sorted, non-overlapping and in range
            for iv in result.intervals:
                if iv.lo > iv.hi or iv.hi >= (1 << coder.total_bits):
                    return _verdict("box_coverage", False, "INVARIANT_MISMATCH",
                                    {"interval": [iv.lo, iv.hi]})
            # The conservative escape must be signalled exactly when the box
            # needs more intervals than the budget.  The boundary box above
            # collapses to a single interval; the alternating points
            # (a even, b=3) are maximally fragmented under the Z-order.
            fragmented = [(0, 6), (3, 3)]  # a in {0,2,4,6}, b pinned to 3
            frag = decompose_box(coder, fragmented, budget=1)
            frag_members = naive_code_members(widths, fragmented)
            if frag_members and not (frag.budget_exhausted and not frag.is_exact):
                return _verdict("box_coverage", False, "INVARIANT_MISMATCH",
                                {"box": "a even / b=3",
                                 "reason": "budget escape not signalled"})
            # ...and with a real budget the same box decomposes exactly.
            frag_ok = decompose_box(coder, fragmented, budget=128)
            if frag_ok.budget_exhausted or not frag_ok.is_exact:
                return _verdict("box_coverage", False, "INVARIANT_MISMATCH",
                                {"box": "a even / b=3",
                                 "reason": "fragmented box should fit budget 128"})
    return _verdict("box_coverage", True, None,
                    {"boxes": len(boxes), "budgets": 3})


def _make_dataset(store: Store, dims: list[DimSpec], points: list[list[int]],
                  name: str, request_id: str) -> None:
    store.initialize(name, [d.to_dict() for d in dims], request_id)
    store.ingest(
        [{d.name: v for d, v in zip(dims, p)} for p in points],
        request_id=f"{request_id}-ingest",
    )


def _zero_miss_case(store: Store, label: str, dims, points, edges,
                    budget: int, rid: str) -> dict | None:
    _make_dataset(store, dims, points, f"verify-{label}", f"{rid}-schema")
    q = store.query(edges, request_id=f"{rid}-query", budget=budget)
    fs = store.full_scan(edges, request_id=f"{rid}-scan")
    q_ids = sorted(r["row_id"] for r in q.rows)
    fs_ids = sorted(r["row_id"] for r in fs["rows"])
    if any(i not in q_ids for i in fs_ids):
        return _verdict(label, False, "RESIDUAL_MISS", {
            "budget": budget,
            "missing_row_ids": [i for i in fs_ids if i not in q_ids][:10],
            "query_stats": q.stats,
        })
    if q_ids != fs_ids:
        extra = [i for i in q_ids if i not in fs_ids]
        return _verdict(label, False, "RESIDUAL_FALSE_ROW", {
            "budget": budget, "false_row_ids": extra[:10],
        })
    # candidates must be a superset; inflation non-negative
    if q.stats["candidate_rows"] < q.stats["result_rows"]:
        return _verdict(label, False, "INVARIANT_MISMATCH",
                        {"stats": q.stats, "reason": "candidates < results"})
    if q.stats["chunks_read"] > q.stats["chunks_total"]:
        return _verdict(label, False, "INVARIANT_MISMATCH",
                        {"stats": q.stats, "reason": "read > total chunks"})
    return None


def check_zero_miss(cfg, make_store) -> dict:
    """Boundary coords, negatives, thin boxes, high-dim sparse vs full scan."""
    rid = "verify-zero-miss"

    # Case 1: 2D signed, boundary values, thin slab around the sign boundary
    dims2 = [DimSpec("x", 5, signed=True), DimSpec("y", 5, signed=True)]
    rng = random.Random(259)
    points2 = [[x, y] for x in (-16, -15, -2, -1, 0, 1, 14, 15)
               for y in (-16, -1, 0, 15)]
    points2 += [[rng.randint(-16, 15), rng.randint(-16, 15)]
                for _ in range(120)]
    edges2 = [{"dimension": "x", "lo": -2, "hi": 1},
              {"dimension": "y", "lo": -1, "hi": 0}]

    # Case 2: 5D sparse; the box is a thin hyperplane (two dims pinned)
    dims5 = [DimSpec(n, 4, signed=(n in ("a", "c")))
             for n in ("a", "b", "c", "d", "e")]
    points5 = []
    for _ in range(300):
        points5.append([rng.choice((-8, -7, -1, 0, 1, 7))
                        if d.signed else rng.choice((0, 1, 14, 15))
                        for d in dims5])
    points5.append([-1, 1, 0, 14, 0])  # guaranteed in-box anchor
    edges5 = [
        {"dimension": "a", "lo": -1, "hi": -1},
        {"dimension": "b", "lo": 1, "hi": 1},
        {"dimension": "c", "lo": 0, "hi": 0},
        {"dimension": "d", "lo": 14, "hi": 15},
        {"dimension": "e", "lo": 0, "hi": 1},
    ]

    detail = {}
    for budget in (1, 8, 256):
        with make_store(cfg) as store:
            err = _zero_miss_case(
                store, f"2d_boundary_negatives_b{budget}",
                dims2, points2, edges2, budget, f"{rid}-2d")
            if err:
                return err
            detail[f"2d_b{budget}"] = None
        with make_store(cfg) as store:
            err = _zero_miss_case(
                store, f"5d_sparse_thin_b{budget}",
                dims5, points5, edges5, budget, f"{rid}-5d")
            if err:
                return err
            detail[f"5d_b{budget}"] = None

    # Capture concrete stats for the report (high budget = exact path)
    with make_store(cfg) as store:
        _make_dataset(store, dims2, points2, "verify-stats", f"{rid}-stats-s")
        outcome = store.query(edges2, request_id=f"{rid}-stats-q", budget=256)
        detail["2d_stats_exact"] = outcome.stats
        outcome_b1 = store.query(edges2, request_id=f"{rid}-stats-q1", budget=1)
        detail["2d_stats_budget1"] = outcome_b1.stats
        # budget escape must be honestly flagged
        if not outcome_b1.budget_exhausted:
            return _verdict("zero_miss", False, "INVARIANT_MISMATCH",
                            {"reason": "budget=1 did not flag exhaustion"})

    return _verdict("zero_miss", True, None, detail)


def check_rowid_stability(cfg, make_store) -> dict:
    """Ingest across many underfilled chunks, compact, identities must survive."""
    rid = "verify-rowid"
    dims = [DimSpec("x", 6, signed=True), DimSpec("y", 6, signed=False)]
    rng = random.Random(2590)
    small_cfg = _override_chunk_size(cfg, 7)
    with make_store(small_cfg) as store:
        store.initialize("stability", [d.to_dict() for d in dims], f"{rid}-schema")
        all_points = [[rng.randint(-32, 31), rng.randrange(64)]
                      for _ in range(100)]
        # ingest in small batches => many small, globally-sorted chunks
        for i in range(0, len(all_points), 3):
            batch = all_points[i:i + 3]
            store.ingest(
                [{dims[0].name: p[0], dims[1].name: p[1]} for p in batch],
                request_id=f"{rid}-ingest-{i}",
            )
        edges = [{"dimension": "x", "lo": -32, "hi": 31},
                 {"dimension": "y", "lo": 0, "hi": 63}]
        before = {r["row_id"]: (r["x"], r["y"])
                  for r in store.full_scan(edges, f"{rid}-scan-before")["rows"]}
        chunks_before = len(store.catalog.list_chunks())
        comp = store.compact(f"{rid}-compact")
        after = {r["row_id"]: (r["x"], r["y"])
                 for r in store.full_scan(edges, f"{rid}-scan-after")["rows"]}
        chunks_after = len(store.catalog.list_chunks())

    if before != after:
        return _verdict("rowid_stability", False, "ROW_ID_UNSTABLE", {
            "changed": [rid_ for rid_ in before if before[rid_] != after.get(rid_)][:10],
            "lost": [rid_ for rid_ in before if rid_ not in after][:10],
        })
    if not chunks_after < chunks_before:
        return _verdict("rowid_stability", False, "INVARIANT_MISMATCH",
                        {"chunks_before": chunks_before,
                         "chunks_after": chunks_after,
                         "reason": "compaction did not reduce chunk count"})
    return _verdict("rowid_stability", True, None,
                    {"rows": len(before),
                     "chunks_before": chunks_before,
                     "chunks_after": chunks_after,
                     "compact": comp["stats"]})


def _override_chunk_size(cfg, size: int):
    import dataclasses
    return dataclasses.replace(cfg, chunk_size=size)


def run_all(cfg, make_store) -> dict:
    checks = [
        check_roundtrip(),
        check_box_coverage(),
        check_zero_miss(cfg, make_store),
        check_rowid_stability(cfg, make_store),
    ]
    return {
        "version": CHECKS_VERSION,
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "failure_categories": [c["failure_category"] for c in checks if not c["ok"]],
    }
