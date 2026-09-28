"""Independent ground-truth reference: full file scan with PyArrow compute.

This module deliberately does NOT import prune.kernel. It re-derives the set of
matching rows by reading every file row-by-row with pyarrow.compute
expressions, so a bug shared by the partition/stat reasoning cannot also hide
here. The validator compares:

  expected_ids = ids matching predicate across ALL files
  planned_ids  = ids matching predicate in files the plan did NOT prune

"Zero missed rows" holds iff expected_ids <= planned_ids. Files the plan kept
but the scan rejects are only wasted I/O (safe); any missed id is a
correctness failure with a concrete category.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Iterable, List, Set

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from . import values as V
from .models import Leaf, Op, Predicate


class ReferenceError(RuntimeError):
    pass


def _lit(value: Any, domain: str):
    v = V.canonical(value, domain)
    if domain == V.DATETIME:
        return pa.scalar(v, type=pa.timestamp("us", tz="UTC"))
    if domain == V.DATE:
        return pa.scalar(_dt.date.fromisoformat(v), type=pa.date32())
    return pa.scalar(v)


def _leaf_expr(leaf: Leaf, domain: str):
    f = pc.field(leaf.column)
    if leaf.op is Op.IS_NULL:
        e = f.is_null()
        return pc.invert(e) if leaf.negated else e
    if leaf.op in (Op.EQ, Op.NE, Op.GT, Op.GE, Op.LT, Op.LE):
        op = {Op.EQ: pc.equal, Op.NE: pc.not_equal, Op.GT: pc.greater,
              Op.GE: pc.greater_equal, Op.LT: pc.less,
              Op.LE: pc.less_equal}[leaf.op]
        return op(f, _lit(leaf.value, domain))
    if leaf.op is Op.BETWEEN:
        return (pc.greater_equal(f, _lit(leaf.value[0], domain))
                & pc.less_equal(f, _lit(leaf.value[1], domain)))
    if leaf.op is Op.IN:
        scalars = [_lit(v, domain) for v in leaf.value]
        value_set = pa.array(scalars, type=scalars[0].type)
        return f.isin(value_set)
    raise ReferenceError(f"unsupported leaf op {leaf.op}")  # pragma: no cover


def _expr(node: Predicate, domains: Dict[str, str]):
    if isinstance(node, Leaf):
        if node.column not in domains:
            raise ReferenceError(f"unknown column {node.column!r}")
        return _leaf_expr(node, domains[node.column])
    es = [_expr(c, domains) for c in node.children]
    # Reduce pairwise: pyarrow boolean kernels are binary. The Expression
    # operators emit kleene (three-valued) AND/OR registered under the names
    # the dataset scanner can bind.
    acc = es[0]
    for e in es[1:]:
        acc = (acc & e) if node.op is Op.AND else (acc | e)
    return acc


def _read_domains(path: str) -> Dict[str, str]:
    schema = pq.ParquetFile(path).schema_arrow
    out = {}
    from .parquet_adapter import pa_type_to_domain
    for name in schema.names:
        dom = pa_type_to_domain(schema.field(name).type)
        if dom:
            out[name] = dom
    return out


def scan_matching_ids(paths: Iterable[str], predicate: Predicate,
                      id_column: str, domains: Dict[str, str]) -> Set[Any]:
    """Return the set of id-column values whose rows match (dedup)."""
    expr = _expr(predicate, domains)
    ids: Set[Any] = set()
    for path in paths:
        table = pq.read_table(path, filters=expr)
        if table.num_rows:
            ids.update(table.column(id_column).to_pylist())
    return ids


def count_rows(paths: Iterable[str], predicate: Predicate,
               domains: Dict[str, str]) -> int:
    expr = _expr(predicate, domains)
    n = 0
    for path in paths:
        n += pq.read_table(path, filters=expr).num_rows
    return n


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

FAILURE_CATEGORIES = (
    "MISSED_ROW",          # a matching row lived in a pruned file
    "PLAN_ERROR",          # planner raised / unknown column etc.
    "REFERENCE_ERROR",     # reference scan itself failed
    "SCHEMA_DRIFT",        # file lacked the id column / domains disagreed
)


def validate_plan_zero_miss(
    *,
    all_paths: List[str],
    kept_paths: List[str],
    pruned_paths: List[str],
    predicate: Predicate,
    id_column: str,
    domains: Dict[str, str],
) -> dict:
    """Compare full scan against the plan's kept files.

    Returns a structured verdict with concrete failure categories; the kernel
    is not consulted to compute the expected answer.
    """
    result = {
        "ok": None,
        "failure_category": None,
        "expected_matching_rows": None,
        "planned_matching_rows": None,
        "missed_ids": [],
        "extra_scan_rows_safe": None,
        "files_all": len(all_paths),
        "files_kept": len(kept_paths),
        "files_pruned": len(pruned_paths),
    }
    try:
        for p in all_paths:
            fdom = _read_domains(p)
            missing = set(domains) - set(fdom)
            if missing:
                raise ReferenceError(f"{p}: missing columns {sorted(missing)}")
        expected = scan_matching_ids(all_paths, predicate, id_column, domains)
        planned = scan_matching_ids(kept_paths, predicate, id_column, domains)
    except ReferenceError as e:
        result.update(ok=False, failure_category="SCHEMA_DRIFT",
                      expected_matching_rows=0, planned_matching_rows=0,
                      detail=str(e))
        return result
    except Exception as e:  # a failure in the reference path is its own category
        result.update(ok=False, failure_category="REFERENCE_ERROR",
                      expected_matching_rows=0, planned_matching_rows=0,
                      detail=repr(e))
        return result

    missed = sorted(expected - planned, key=lambda x: (str(type(x)), x))
    # ids in planned but not expected can't happen (filters are identical),
    # but report any scan noise honestly.
    result.update(
        expected_matching_rows=len(expected),
        planned_matching_rows=len(planned),
        missed_ids=missed[:200],
        extra_scan_rows_safe=max(0, len(planned) - len(expected)),
    )
    if missed:
        result.update(ok=False, failure_category="MISSED_ROW")
    else:
        result.update(ok=True, failure_category=None)
    return result
