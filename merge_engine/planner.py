"""Pure decision core: source rows + target snapshot -> validated MergePlan.

The planner executes **no SQL** and never touches the database. Its inputs are
the normalized spec, canonical source rows and the materialized pre-operation
target snapshot. That boundary is what makes the required invariants
structural rather than hoped-for:

* matched rows are found in ``target_rows`` only - a row inserted earlier in
  the batch exists solely in the plan and cannot be matched by a later row;
* source-internal duplicate keys are rejected on a hash grouping, so the
  result cannot depend on accidental row order (rows are additionally
  processed in a deterministic key order);
* every condition *and* every action value is evaluated here; if anything
  raises, no plan is returned and nothing is committed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contract import (
    Decision,
    MergePlan,
    MergeSpec,
    NullPolicy,
    Outcome,
    PlannedAction,
    SourceRow,
    TargetRow,
    WhenClause,
    ClauseType,
)
from .errors import (
    SOURCE_DUPLICATE_KEY_CODE,
    SPEC_INVALID_CODE,
    InputError,
)
from .predicate import (
    CompiledExpr,
    Namespace,
    PredicateError,
    compile_expression,
    compile_predicate,
    evaluation_failure,
)
from .snapshot import assert_no_duplicate_target_keys
from .utils import key_group_token, key_sort_token

_MAX_ERROR_DETAILS = 50


@dataclass(frozen=True)
class CompiledClause:
    clause: WhenClause
    condition: CompiledExpr | None
    assignments: dict[str, CompiledExpr]


@dataclass(frozen=True)
class PreparedSpec:
    spec: MergeSpec
    compiled: tuple[CompiledClause, ...]
    source_columns: frozenset[str]
    target_columns: frozenset[str]
    not_null_target_columns: frozenset[str]

    def needed_target_columns(self) -> list[str]:
        """Columns that must be read from the target snapshot."""
        needed = set(self.spec.key_columns)
        needed.update(self.spec.update_columns)
        needed.update(self.spec.insert_columns)
        for cc in self.compiled:
            if cc.condition is not None:
                needed.update(_referenced_target_cols(cc.condition))
            for expr in cc.assignments.values():
                needed.update(_referenced_target_cols(expr))
        # preserve target schema order for readable SQL
        return [c for c in sorted(needed)]


def _referenced_target_cols(expr: CompiledExpr) -> set[str]:
    import ast

    cols: set[str] = set()
    for node in ast.walk(expr.tree):
        if isinstance(node, ast.Name) and node.id.startswith("T."):
            cols.add(node.id[2:])
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "T":
                cols.add(node.attr)
    return cols


def prepare(
    spec: MergeSpec,
    source_columns: list[str],
    target_columns: list[str],
    *,
    not_null_target_columns: set[str] | frozenset[str] | None = None,
) -> PreparedSpec:
    """Compile every expression and validate column contracts.

    Raises InputError for unknown/ambiguous columns, action/column mismatches
    and missing key columns. Raises ComputationFailureError never here -
    evaluation errors are data-dependent and surface in :func:`build_plan`.
    """
    src = frozenset(source_columns)
    tgt = frozenset(target_columns)
    ns = Namespace(source_cols=src, target_cols=tgt)

    missing_src_keys = [c for c in spec.key_columns if c not in src]
    if missing_src_keys:
        raise InputError(
            SPEC_INVALID_CODE,
            "key columns missing from source schema",
            {"missing": missing_src_keys},
        )
    missing_tgt_keys = [c for c in spec.key_columns if c not in tgt]
    if missing_tgt_keys:
        raise InputError(
            SPEC_INVALID_CODE,
            "key columns missing from target schema",
            {"missing": missing_tgt_keys},
        )

    for col in spec.update_columns:
        if col not in tgt:
            raise InputError(
                SPEC_INVALID_CODE,
                f"update column {col!r} does not exist in target",
                {"column": col},
            )
    for col in spec.insert_columns:
        if col not in tgt:
            raise InputError(
                SPEC_INVALID_CODE,
                f"insert column {col!r} does not exist in target",
                {"column": col},
            )

    compiled: list[CompiledClause] = []
    for clause in spec.clauses:
        allow_target = clause.matched
        label = f"when_clauses[{clause.order}].condition"
        cond = compile_predicate(
            clause.condition, ns, allow_target=allow_target, label=label
        )
        compiled_assignments: dict[str, CompiledExpr] = {}
        allowed_targets = (
            set(spec.update_columns)
            if clause.type == ClauseType.MATCHED_UPDATE
            else set(spec.insert_columns)
        )
        for col, text in clause.assignments.items():
            if col not in allowed_targets:
                kind = "update_columns" if clause.matched else "insert_columns"
                raise InputError(
                    SPEC_INVALID_CODE,
                    f"when_clauses[{clause.order}] assigns {col!r}, which is "
                    f"not declared in {kind}",
                    {"column": col, "declared": sorted(allowed_targets)},
                )
            compiled_assignments[col] = compile_expression(
                text,
                ns,
                allow_target=allow_target,
                label=f"when_clauses[{clause.order}].assignments[{col!r}]",
            )
        compiled.append(CompiledClause(clause, cond, compiled_assignments))

    return PreparedSpec(
        spec=spec,
        compiled=tuple(compiled),
        source_columns=src,
        target_columns=tgt,
        not_null_target_columns=frozenset(not_null_target_columns or ()),
    )


# ------------------------------------------------------------------ build
def build_plan(
    prepared: PreparedSpec,
    source_rows: list[SourceRow],
    target_rows: list[TargetRow],
    *,
    source_fingerprint: str,
    target_fingerprint: str,
) -> MergePlan:
    """Validate all data-level preconditions and decide every action.

    Order of checks (all failures abort before any SQL runs):
    1. target duplicate keys (state conflict)
    2. source duplicate keys (input conflict)
    3. all predicates and action values (computation failures, collected)
    """
    spec = prepared.spec

    # 1) target must offer at most one match per key
    assert_no_duplicate_target_keys(target_rows, spec)

    # 2) source must define at most one intent per key
    _assert_unique_source_keys(source_rows, spec)

    # 3) decisions
    match_index = _build_match_index(target_rows, spec)

    ordered = sorted(
        source_rows,
        key=lambda r: (key_sort_token(_source_key(r, spec)), r.index),
    )

    actions: list[PlannedAction] = []
    decisions: list[Decision] = []
    failures: list[dict[str, Any]] = []
    seq = 0

    for srow in ordered:
        key = _source_key(srow, spec)
        target = _lookup_match(match_index, key, spec)
        matched = target is not None
        env = _environment(srow, target)

        clause_trace: list[dict[str, Any]] = []
        chosen: CompiledClause | None = None
        clause_failures: list[dict[str, Any]] = []

        for cc in prepared.compiled:
            if cc.clause.matched != matched:
                continue
            result, err = _evaluate_condition(cc, env)
            clause_trace.append(
                {
                    "clause": cc.clause.order,
                    "matched_side": "MATCHED" if cc.clause.matched else "NOT_MATCHED",
                    "condition": cc.clause.condition,
                    "result": result,
                }
            )
            if err is not None:
                clause_failures.append(err)
                continue
            if result is True and chosen is None:
                chosen = cc

        failures.extend(clause_failures)
        if failures and len(failures) >= _MAX_ERROR_DETAILS:
            break

        if chosen is None:
            outcome = Outcome.UNPROCESSED
            fired = None
            reason = _no_fire_reason(matched, clause_trace, bool(clause_failures))
            new_values: dict[str, Any] = {}
            rowid = target.rowid if target is not None else None
        else:
            outcome, new_values, value_error = _materialize_action(
                chosen, srow, env, prepared
            )
            if value_error is not None:
                failures.append(value_error)
                if len(failures) >= _MAX_ERROR_DETAILS:
                    break
                continue
            fired = chosen.clause.order
            rowid = target.rowid if target is not None else None
            reason = (
                f"first qualifying {('MATCHED' if matched else 'NOT_MATCHED')} "
                f"clause #{fired} ({outcome.value}); rules evaluated in order, "
                "first true condition wins"
            )
            seq += 1
            actions.append(
                PlannedAction(
                    seq=seq,
                    outcome=outcome,
                    source_index=srow.index,
                    target_rowid=rowid,
                    key=key,
                    new_values=new_values,
                )
            )

        decisions.append(
            Decision(
                source_index=srow.index,
                key=key,
                matched=matched,
                target_rowid=rowid,
                outcome=outcome,
                fired_clause=fired,
                reason=reason,
                clause_results=tuple(clause_trace),
            )
        )

    if failures:
        raise evaluation_failure(
            f"{len(failures)} expression(s) failed while validating the batch; "
            "no action was executed",
            details={"errors": failures[:_MAX_ERROR_DETAILS], "truncated": len(failures) > _MAX_ERROR_DETAILS},
        )

    return MergePlan(
        spec=spec,
        actions=actions,
        decisions=decisions,
        source_count=len(source_rows),
        target_count=len(target_rows),
        source_fingerprint=source_fingerprint,
        target_fingerprint=target_fingerprint,
        target_rows=list(target_rows),
    )


# ------------------------------------------------------------- source checks
def _source_key(row: SourceRow, spec: MergeSpec) -> tuple[Any, ...]:
    return tuple(row.values.get(k) for k in spec.key_columns)


def _assert_unique_source_keys(rows: list[SourceRow], spec: MergeSpec) -> None:
    groups: dict[Any, list[SourceRow]] = {}
    for row in rows:
        key = _source_key(row, spec)
        if spec.null_policy == NullPolicy.SQL_DISTINCT and any(v is None for v in key):
            continue
        groups.setdefault(key_group_token(key), []).append(row)

    dups: list[dict[str, Any]] = []
    for token, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (r.index,))
        dups.append(
            {
                "key": list(token),
                "count": len(members),
                "source_indexes": [r.index for r in members],
            }
        )
    if dups:
        dups.sort(key=lambda d: (key_sort_token(d["key"]), d["source_indexes"]))
        raise InputError(
            SOURCE_DUPLICATE_KEY_CODE,
            f"source contains {len(dups)} composite-key group(s) with more than "
            f"one row under policy {spec.null_policy.value}; each key must have "
            "exactly one source intent",
            {"duplicate_groups": dups[:10], "truncated": len(dups) > 10},
        )


# ------------------------------------------------------------- target lookup
def _target_key(row: TargetRow, spec: MergeSpec) -> tuple[Any, ...]:
    return tuple(row.values[k] for k in spec.key_columns)


def _build_match_index(
    rows: list[TargetRow], spec: MergeSpec
) -> dict[Any, TargetRow]:
    index: dict[Any, TargetRow] = {}
    for row in rows:
        key = _target_key(row, spec)
        if spec.null_policy == NullPolicy.SQL_DISTINCT and any(v is None for v in key):
            continue
        index[key_group_token(key)] = row
    return index


def _lookup_match(
    index: dict[Any, TargetRow], key: tuple[Any, ...], spec: MergeSpec
) -> TargetRow | None:
    if spec.null_policy == NullPolicy.SQL_DISTINCT and any(v is None for v in key):
        return None
    return index.get(key_group_token(key))


# ---------------------------------------------------------------- evaluate
def _environment(srow: SourceRow, trow: TargetRow | None) -> dict[str, Any]:
    env = {f"S.{k}": v for k, v in srow.values.items()}
    if trow is not None:
        env.update({f"T.{k}": v for k, v in trow.values.items()})
    return env


def _evaluate_condition(
    cc: CompiledClause, env: dict[str, Any]
) -> tuple[Any, dict[str, Any] | None]:
    if cc.condition is None:
        return True, None
    try:
        raw = cc.condition.evaluate(env)
    except PredicateError as exc:
        return None, {
            "clause": cc.clause.order,
            "where": "condition",
            "expression": cc.clause.condition,
            "error": exc.message,
        }
    if raw is None:
        return None, None  # SQL UNKNOWN - trace carries None
    if isinstance(raw, (bool, int, float)):
        return bool(raw), None
    return None, {
        "clause": cc.clause.order,
        "where": "condition",
        "expression": cc.clause.condition,
        "error": f"condition returned non-boolean {type(raw).__name__}",
    }


def _no_fire_reason(
    matched: bool, trace: list[dict[str, Any]], had_failure: bool
) -> str:
    side = "MATCHED" if matched else "NOT_MATCHED"
    if not trace:
        return f"no {side} WHEN rule is defined for this row"
    if had_failure:
        return f"{side} rule(s) present but condition evaluation raised"
    nulls = sum(1 for t in trace if t["result"] is None)
    falses = sum(1 for t in trace if t["result"] is False)
    return (
        f"{side} rule(s) present but none fired "
        f"({falses} false, {nulls} unknown/NULL conditions); "
        "rule priority is list order, first true wins"
    )


def _materialize_action(
    cc: CompiledClause,
    srow: SourceRow,
    env: dict[str, Any],
    prepared: PreparedSpec,
) -> tuple[Outcome, dict[str, Any], dict[str, Any] | None]:
    spec = prepared.spec
    ctype = cc.clause.type

    evaluated: dict[str, Any] = {}
    for col, expr in cc.assignments.items():
        try:
            evaluated[col] = _check_scalar(expr.evaluate(env), col)
        except PredicateError as exc:
            return (
                Outcome.UNPROCESSED,
                {},
                {
                    "clause": cc.clause.order,
                    "where": f"assignment:{col}",
                    "expression": cc.clause.assignments[col],
                    "error": exc.message,
                },
            )

    if ctype == ClauseType.MATCHED_DELETE:
        return Outcome.DELETE, {}, None

    if ctype == ClauseType.MATCHED_UPDATE:
        for col, val in evaluated.items():
            if col in prepared.not_null_target_columns and val is None:
                return (
                    Outcome.UNPROCESSED,
                    {},
                    {
                        "clause": cc.clause.order,
                        "where": f"assignment:{col}",
                        "expression": cc.clause.assignments[col],
                        "error": f"NOT NULL target column {col!r} would be set to NULL",
                    },
                )
        return Outcome.UPDATE, evaluated, None

    # INSERT: key columns copied from the source row, then declared inserts
    row_values: dict[str, Any] = {}
    for k in spec.key_columns:
        row_values[k] = _check_scalar(srow.values.get(k), k)
    row_values.update(evaluated)

    # A NOT NULL-without-default column that the spec never even maps cannot
    # be satisfied by this INSERT (no value source exists). Columns the spec
    # maps but this particular clause omits are left to the database default
    # / executor constraint check.
    declared = set(spec.key_columns) | set(spec.insert_columns)
    truly_missing = sorted(c for c in prepared.not_null_target_columns if c not in declared)
    if truly_missing:
        return (
            Outcome.UNPROCESSED,
            {},
            {
                "clause": cc.clause.order,
                "where": "insert-shape",
                "expression": None,
                "error": (
                    "INSERT does not supply NOT NULL column(s) "
                    f"{truly_missing} and no spec mapping exists"
                ),
            },
        )
    for col, val in row_values.items():
        if col in prepared.not_null_target_columns and val is None:
            return (
                Outcome.UNPROCESSED,
                {},
                {
                    "clause": cc.clause.order,
                    "where": f"assignment:{col}",
                    "expression": cc.clause.assignments.get(col, f"S.{col}"),
                    "error": f"NOT NULL target column {col!r} would be inserted as NULL",
                },
            )
    return Outcome.INSERT, row_values, None


def _check_scalar(v: Any, col: str) -> Any:
    if v is None or isinstance(v, (bool, int, float, str, bytes)):
        return v
    if isinstance(v, (list, tuple, dict)):
        raise PredicateError(
            f"value for {col!r} is {type(v).__name__}; only scalar values bind to SQL"
        )
    raise PredicateError(f"value for {col!r} has unsupported type {type(v).__name__}")
