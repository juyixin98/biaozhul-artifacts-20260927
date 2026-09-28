#!/usr/bin/env python3
"""Preserved validation process.

Runs the required checks against an INDEPENDENT brute-force reference (naive
per-coordinate loop over the raw generated rows - never the kernel under
test) and reports, per scenario:

  * encode/decode round-trip on boundary coordinates (incl. negatives),
  * zero missed rows and zero false rows vs. full scan,
  * candidate inflation (code candidates / true matches) across budgets,
  * chunk reads: selected vs. total chunks and physical bytes read,
  * explicit pass/fail plus an "uncertainties" list for non-fatal conditions.

Usage:
    .venv/bin/python scripts/validate.py [--data-dir DIR] [--out results/validation.json]

Exit code is non-zero if any required check fails.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zindex.catalog import Catalog  # noqa: E402
from zindex.chunkstore import ROW_ID_COL  # noqa: E402
from zindex.encoding import DimSpec, SchemaSpec, decode, encode  # noqa: E402
from zindex.fixtures import generate_rows  # noqa: E402
from zindex.ingest import IngestService  # noqa: E402
from zindex.kernel import Kernel  # noqa: E402


def brute_force(rows, lo, hi):
    """Independent reference: plain nested comparisons on raw coordinates."""
    return {
        i for i, row in enumerate(rows)
        if all(a <= int(v) <= b for v, a, b in zip(row, lo, hi))
    }


def check_roundtrip(schema: SchemaSpec, boundary_rows) -> dict:
    failures = []
    checked = 0
    for row in boundary_rows:
        code = encode(tuple(row), schema)
        back = decode(code, schema)
        checked += 1
        if back != tuple(row):
            failures.append({"coords": list(row), "code": str(code), "decoded": list(back)})
        if code >> schema.total_bits:
            failures.append({"coords": list(row), "problem": "code exceeded declared width"})
    return {"checked": checked, "failures": failures,
            "passed": not failures}


def boundaries_for(schema: SchemaSpec):
    import itertools
    # one coordinate at each extreme, plus all-min/all-max corners and zeros
    lo = [-(1 << (d.bits - 1)) if d.signed else 0 for d in schema.dims]
    hi = [(1 << (d.bits - 1)) - 1 if d.signed else (1 << d.bits) - 1 for d in schema.dims]
    rows = {tuple(lo), tuple(hi), tuple(0 for _ in schema.dims)}
    for j, d in enumerate(schema.dims):
        rows.add(tuple(hi[j] if i == j else 0 for i, d in enumerate(schema.dims)))
        rows.add(tuple(lo[j] if i == j else 0 for i, d in enumerate(schema.dims)))
    # exhaustive for tiny 2-3D schemas, otherwise a bounded sample
    total = sum(d.bits for d in schema.dims)
    if total <= 10:
        ranges = [range(lo[i], hi[i] + 1) for i in range(len(schema.dims))]
        for r in itertools.product(*ranges):
            rows.add(r)
    return sorted(rows)


def data_derived_boxes(rows, dims, *, thin_frac=0.04, cloud_point=None, cloud_radius=None):
    """Build representative boxes FROM the data so every box has hits.

    Returns a list of (label, lo, hi): a broad center box, per-axis thin
    slices through the middle, and a tight box around one real row. Coords
    are raw (signed) values, matching the API contract.

    For high-dimensional sparse clouds the quartile box is usually empty
    (joint sparsity), so callers may pass ``cloud_point`` (a guaranteed real
    row) and ``cloud_radius`` to anchor a non-empty neighborhood box there.
    """
    import statistics
    bounds = []
    for dd in dims:
        if dd.get("signed", True):
            bounds.append((-(1 << (dd["bits"] - 1)), (1 << (dd["bits"] - 1)) - 1))
        else:
            bounds.append((0, (1 << dd["bits"]) - 1))
    clamp = lambda j, v: min(bounds[j][1], max(bounds[j][0], int(v)))
    cols = [sorted(r[j] for r in rows) for j in range(len(dims))]
    q = lambda c, f: c[min(len(c) - 1, int(len(c) * f))]
    broad_lo = [q(c, 0.25) for c in cols]
    broad_hi = [q(c, 0.75) for c in cols]
    if cloud_point is not None:
        # non-empty broad-ish box anchored at a real cluster point
        if cloud_radius is None:
            cloud_radius = max(1, int(max(q(c, 0.9) - q(c, 0.1) for c in cols) * 0.1))
        rad = cloud_radius
        boxes = [("cloud_neighborhood",
                  [clamp(j, v - rad) for j, v in enumerate(cloud_point)],
                  [clamp(j, v + rad) for j, v in enumerate(cloud_point)])]
    else:
        boxes = [("iqr_broad", broad_lo, broad_hi)]
    # thin slice: fix every dim to a +/-window around a real central point
    if cloud_point is not None:
        anchor = list(cloud_point)
    else:
        # choose the data point nearest the coordinate-wise median, so the
        # "needle" box is guaranteed to contain that real row
        med = [int(statistics.median(c)) for c in cols]
        anchor = list(min(rows, key=lambda r: sum(abs(r[j] - med[j]) for j in range(len(dims)))))
    span = max(cols[0]) - min(cols[0])
    half = max(1, int(span * thin_frac))
    # ensure the thin box contains at least its anchor point exactly
    half = max(half, 0)
    boxes.append((
        "thin_center",
        [clamp(j, m - half) for j, m in enumerate(anchor)],
        [clamp(j, m + half) for j, m in enumerate(anchor)],
    ))
    # one thin axis, others broad
    for j, d in enumerate(dims):
        lo = list(broad_lo)
        hi = list(broad_hi)
        lo[j] = clamp(j, anchor[j] - 1)
        hi[j] = clamp(j, anchor[j] + 1)
        boxes.append((f"sliver_on_{d['name']}", lo, hi))
    # tight box around the anchor data point: exactly that row (non-empty)
    boxes.append(("needle_at_data_point", list(anchor), list(anchor)))
    return boxes


def run_scenario(name, dims, n, shape, seed, boxes, budgets, capacity, data_dir):
    spec = SchemaSpec(name=name, dims=tuple(DimSpec(**d) for d in dims))
    rows = generate_rows(spec, n, shape=shape, seed=seed)
    catalog_path = data_dir / f"cat_{name}.sqlite"
    catalog = Catalog(catalog_path)
    ing = IngestService(catalog, data_dir, default_capacity=capacity)
    ing.create_schema(name, dims, overwrite=True)
    t0 = time.perf_counter()
    ing.ingest_rows(name, rows)
    ingest_ms = (time.perf_counter() - t0) * 1000
    # rewrite to materialize globally sorted, disjoint chunks
    ing.rewrite_all(name, capacity=capacity)
    ker = Kernel(catalog, data_dir)

    rt = check_roundtrip(spec, boundaries_for(spec))
    box_reports = []
    all_passed = rt["passed"]
    uncertainties = []

    for label, lo, hi in boxes:
        truth = brute_force(rows, lo, hi)
        per_budget = []
        for budget in budgets:
            out = ker.query(name, lo, hi, budget)
            got = {r[ROW_ID_COL] for r in out.rows}
            missed = sorted(truth - got)
            extra = sorted(got - truth)
            passed = not missed and not extra
            all_passed &= passed
            stats = out.stats
            per_budget.append({
                "budget": budget,
                "true_matches": len(truth),
                "code_candidates": stats["code_candidates"],
                "candidate_bloat_ratio": stats["candidate_bloat_ratio"],
                "intervals": stats["intervals"],
                "exact_intervals": stats["exact_intervals"],
                "conservative_intervals": stats["conservative_intervals"],
                "budget_exhausted": stats["budget_exhausted"],
                "chunks_total": stats["chunks_total"],
                "chunks_selected": stats["chunks_selected"],
                "chunks_skipped": stats["chunks_skipped"],
                "chunk_bytes_read": stats["chunk_bytes_read"],
                "missed_rows": len(missed),
                "false_rows": len(extra),
                "passed": passed,
            })
            if out.status == "degraded":
                uncertainties.append({"box": label, "detail": out.uncertainties})
        box_reports.append({"label": label, "lo": lo, "hi": hi, "by_budget": per_budget})

    # stable row identity across a second rewrite
    sample_box = boxes[0]
    before = {r[ROW_ID_COL]: tuple(r[d["name"]] for d in dims)
              for r in ker.query(name, sample_box[1], sample_box[2], 4096).rows}
    ing.rewrite_all(name, capacity=capacity // 2 if capacity > 1 else 1)
    after = {r[ROW_ID_COL]: tuple(r[d["name"]] for d in dims)
             for r in ker.query(name, sample_box[1], sample_box[2], 4096).rows}
    identity_passed = before == after
    all_passed &= identity_passed

    catalog.close()
    return {
        "scenario": name,
        "shape": shape,
        "n_rows": len(rows),
        "dimensions": dims,
        "total_bits": spec.total_bits,
        "chunk_capacity": capacity,
        "ingest_ms": round(ingest_ms, 2),
        "roundtrip": rt,
        "boxes": box_reports,
        "stable_row_identity_after_rewrite": identity_passed,
        "uncertainties": uncertainties,
        "passed": all_passed,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/validation")
    ap.add_argument("--out", default="results/validation.json")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    scenarios = []

    # 1) 2D signed 8-bit: boundary coords, negatives, thin boxes
    _dims1 = [{"name": "x", "bits": 8, "signed": True},
              {"name": "y", "bits": 8, "signed": True}]
    _rows1 = generate_rows(SchemaSpec(dims=tuple(DimSpec(**d) for d in _dims1)),
                           20000, shape="uniform", seed=20260928)
    scenarios.append(run_scenario(
        "s2d", _dims1,
        n=20000, shape="uniform", seed=20260928, capacity=4000,
        data_dir=data_dir,
        budgets=[1, 16, 256, 4096],
        boxes=data_derived_boxes(_rows1, _dims1) + [
            ("negative_corner_canonical", [-128, -128], [-124, -124]),
            ("guaranteed_empty_point", [127, 126], [127, 126]),
        ],
    ))

    # 2) 4D signed 5-bit sparse cloud incl. thin high-dimensional boxes
    _dims2 = [{"name": f"d{i}", "bits": 5, "signed": True} for i in range(4)]
    _rows2 = generate_rows(SchemaSpec(dims=tuple(DimSpec(**d) for d in _dims2)),
                           12000, shape="sparse_hd", seed=4242)
    scenarios.append(run_scenario(
        "s4d_sparse", _dims2,
        n=12000, shape="sparse_hd", seed=4242, capacity=2000,
        data_dir=data_dir,
        budgets=[1, 8, 128, 4096],
        boxes=data_derived_boxes(
            _rows2, _dims2, cloud_point=list(_rows2[0]), cloud_radius=4
        ) + [
            ("whole_universe", [-16] * 4, [15] * 4),
        ],
    ))

    # 3) 8D x 16-bit = 128 interleaved bits: high bits must survive, sparse
    _dims3 = [{"name": f"d{i}", "bits": 16, "signed": True} for i in range(8)]
    _rows3 = generate_rows(SchemaSpec(dims=tuple(DimSpec(**d) for d in _dims3)),
                           6000, shape="sparse_hd", seed=8848)
    scenarios.append(run_scenario(
        "s8d_128bit", _dims3,
        n=6000, shape="sparse_hd", seed=8848, capacity=1500,
        data_dir=data_dir,
        budgets=[1, 64, 4096],
        boxes=data_derived_boxes(
            _rows3, _dims3, thin_frac=0.001,
            cloud_point=list(_rows3[0]), cloud_radius=300,
        ) + [
            ("whole_universe", [-32768] * 8, [32767] * 8),
        ],
    ))

    # 4) mixed signedness / unequal widths coverage
    _dims4 = [{"name": "a", "bits": 3, "signed": True},
              {"name": "b", "bits": 6, "signed": False},
              {"name": "c", "bits": 4, "signed": True}]
    _rows4 = generate_rows(SchemaSpec(dims=tuple(DimSpec(**d) for d in _dims4)),
                           4000, shape="uniform", seed=77)
    scenarios.append(run_scenario(
        "s_mixed", _dims4,
        n=4000, shape="uniform", seed=77, capacity=1000,
        data_dir=data_dir,
        budgets=[1, 32, 4096],
        boxes=data_derived_boxes(_rows4, _dims4),
    ))

    overall_passed = all(s["passed"] for s in scenarios)
    report = {
        "validation": "morton-clustered-range-query",
        "passed": overall_passed,
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)

    # concise human summary
    print(f"validation overall: {'PASS' if overall_passed else 'FAIL'}")
    for s in scenarios:
        print(f"\n[{s['scenario']}] bits={s['total_bits']} rows={s['n_rows']} "
              f"chunks_cap={s['chunk_capacity']} -> {'PASS' if s['passed'] else 'FAIL'}")
        print(f"  roundtrip: {s['roundtrip']['checked']} boundary points, "
              f"failures={len(s['roundtrip']['failures'])}; "
              f"stable_row_id={s['stable_row_identity_after_rewrite']}")
        for b in s["boxes"]:
            line = f"  {b['label']}:"
            for r in b["by_budget"]:
                bloat = "  n/a " if r["candidate_bloat_ratio"] is None else f"{r['candidate_bloat_ratio']:>7}"
                line += (
                    f" | budget {r['budget']:>4}: truth={r['true_matches']:>5} "
                    f"cand={r['code_candidates']:>6} bloat={bloat} "
                    f"chunks={r['chunks_selected']}/{r['chunks_total']} "
                    f"bytes={r['chunk_bytes_read']:>8} miss={r['missed_rows']} "
                    f"extra={r['false_rows']}"
                )
            print(line)
    print(f"\nreport written to {args.out}")
    return 0 if overall_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
