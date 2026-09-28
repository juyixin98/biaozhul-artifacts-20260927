"""Service layer: wires config + catalog + kernel + reference checker."""

from __future__ import annotations

from typing import Any

from .catalog import Catalog
from .config import Config, TableSpec
from .kernel import (Plan, TableContext, UnknownColumnError, plan_prune)
from .logctx import Trace
from .models import Predicate, PredicateError, parse_predicate, walk
from .reference import validate_plan_zero_miss


class RequestError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def validate_columns(predicate: Predicate, ctx: TableContext) -> None:
    missing = sorted({leaf.column for leaf in walk(predicate)
                      if leaf.column not in ctx.columns})
    if missing:
        raise UnknownColumnError(", ".join(missing))


def build_context(config: Config, catalog: Catalog, table: str) \
        -> tuple[TableSpec, TableContext]:
    spec = config.tables.get(table)
    if spec is None:
        raise RequestError("UNKNOWN_TABLE", f"table {table!r} is not configured", 404)
    try:
        ctx = catalog.load_context(spec, table)
    except KeyError:
        raise RequestError("NOT_REFRESHED",
                           f"table {table!r} has no metadata; run refresh", 409)
    return spec, ctx


def parse_or_raise(payload: Any) -> Predicate:
    try:
        return parse_predicate(payload)
    except PredicateError as e:
        raise RequestError("BAD_PREDICATE", str(e), 400)


def run_plan(config: Config, catalog: Catalog, table: str, payload: Any,
             trace: Trace) -> Plan:
    spec, ctx = build_context(config, catalog, table)
    predicate = parse_or_raise(payload)
    try:
        validate_columns(predicate, ctx)
    except UnknownColumnError as e:
        trace.failure("UNKNOWN_COLUMN", f"predicate references unknown column(s): {e}",
                      location="kernel")
        raise RequestError("UNKNOWN_COLUMN", str(e), 400)

    trace.step("load_context", "catalog metadata loaded",
               location="sqlite", partitions=len(ctx.partitions))

    try:
        plan = plan_prune(ctx, predicate, trace.request_id)
    except (UnknownColumnError,) as e:  # defensive; pre-validated above
        raise RequestError("UNKNOWN_COLUMN", str(e), 400)

    trace.versions({
        "transform_spec": plan.transform_version,
        "tzdb": plan.tzdb_version,
        "stats_schema": plan.stats_schema_version,
    }, location="kernel")
    trace.step("level1_partitions", "directory pruning complete",
               location="kernel", **{k: plan.metrics[k] for k in
                                     ("partitions_total", "partitions_pruned")})
    trace.step("level2_files", "file-stat pruning complete",
               location="kernel", **{k: plan.metrics[k] for k in
                                     ("files_total", "files_pruned",
                                      "rows_pruned", "bytes_pruned")})
    for u in plan.uncertain:
        trace.uncertain(u["code"], u["detail"],
                        location=u.get("target"), leaf=u.get("leaf"))
    for f in plan.failures:
        trace.failure(f["code"], f["detail"], location="kernel")
    return plan


def _plan_paths(plan: Plan):
    all_paths, kept_paths, pruned_paths = [], [], []
    for p in plan.partitions:
        for f in p.files:
            all_paths.append(f.path)
            (kept_paths if f.verdict != "PRUNED" else pruned_paths).append(f.path)
    return all_paths, kept_paths, pruned_paths


def run_validation(config: Config, catalog: Catalog, table: str, payload: Any,
                   trace: Trace, id_column: str) -> dict:
    plan = run_plan(config, catalog, table, payload, trace)
    spec, ctx = build_context(config, catalog, table)
    if id_column not in ctx.columns:
        raise RequestError("UNKNOWN_ID_COLUMN",
                           f"id column {id_column!r} not in schema", 400)

    all_paths, kept_paths, pruned_paths = _plan_paths(plan)
    trace.step("reference_scan", "full PyArrow scan of every file",
               location="reference", files=len(all_paths))
    domains = {name: col.type for name, col in ctx.columns.items()}
    verdict = validate_plan_zero_miss(
        all_paths=all_paths, kept_paths=kept_paths,
        pruned_paths=pruned_paths, predicate=parse_or_raise(payload),
        id_column=id_column, domains=domains)
    if verdict["ok"]:
        trace.step("zero_miss", "no matching row lives in a pruned file",
                   location="reference", expected=verdict["expected_matching_rows"])
    else:
        trace.failure(verdict["failure_category"],
                      f"validation failed: {verdict.get('detail', '')}",
                      location="reference")
    return {"validation": verdict}
