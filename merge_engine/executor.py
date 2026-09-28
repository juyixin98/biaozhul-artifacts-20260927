"""Atomic execution of a validated MergePlan.

Contract with the planner: the executor receives a fully validated plan. It
opens ONE ``BEGIN IMMEDIATE`` transaction, applies actions in plan order, and
commits once. Any failure rolls the whole transaction back - SQLite itself
guarantees no partial update. Two extra guarantees are enforced explicitly:

* **snapshot staleness check** - before applying an UPDATE/DELETE the current
  values of the target row are compared with the snapshot that drove the
  decision. A concurrent committed change aborts as STATE_CONFLICT before any
  write in this transaction.
* **injected commit failure** - ``failpoint_commit`` raises a
  RESOURCE_EXHAUSTED/COMMIT_FAILED error at commit time; tests assert that
  afterwards the target is byte-for-byte unchanged (rollback path).

SQLite error mapping:
    IntegrityError -> STATE_CONFLICT / CONSTRAINT_VIOLATION
    locked/busy    -> RESOURCE_EXHAUSTED / DB_LOCKED
    other          -> COMPUTATION_FAILURE
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from .contract import MergePlan, Outcome
from .errors import (
    COMMIT_FAILED_CODE,
    CONSTRAINT_VIOLATION_CODE,
    DB_LOCKED_CODE,
    PREDICATE_FAILED_CODE,
    SNAPSHOT_STALE_CODE,
    ComputationFailureError,
    ResourceExhaustedError,
    StateConflictError,
)
from .utils import canonical_json, quote_ident


def execute_plan(
    conn: sqlite3.Connection,
    plan: MergePlan,
    *,
    failpoint: str | None = None,
    on_action: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    """Run the plan atomically. Returns {'update':n,'insert':n,'delete':n}."""
    spec = plan.spec
    snapshot_by_rowid = {r.rowid: r.values for r in plan.target_rows}
    watch_cols = sorted(
        set(spec.key_columns) | set(spec.update_columns)
        | {
            c
            for a in plan.actions
            for c in a.new_values
        }
    )

    counts = {"update": 0, "insert": 0, "delete": 0}
    tx_started = False
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            tx_started = True
        except sqlite3.OperationalError as exc:
            if _is_locked(exc):
                raise ResourceExhaustedError(
                    DB_LOCKED_CODE,
                    f"could not acquire write lock to begin merge: {exc}",
                )
            raise

        for action in plan.actions:
            if action.outcome in (Outcome.UPDATE, Outcome.DELETE):
                _assert_not_stale(conn, spec, action, snapshot_by_rowid, watch_cols)

            if action.outcome == Outcome.UPDATE:
                sql, params = _build_update(spec, action)
                _run(conn, sql, params)
                counts["update"] += 1
            elif action.outcome == Outcome.INSERT:
                sql, params = _build_insert(spec, action)
                _run(conn, sql, params)
                counts["insert"] += 1
            else:  # DELETE
                sql = (
                    f"DELETE FROM {quote_ident(spec.target_table)} "
                    "WHERE rowid = ?"
                )
                _run(conn, sql, (action.target_rowid,))
                counts["delete"] += 1

            if on_action is not None:
                on_action(
                    action.outcome.value,
                    {"seq": action.seq, "target_rowid": action.target_rowid},
                )

        if failpoint == "commit":
            raise ResourceExhaustedError(
                COMMIT_FAILED_CODE,
                "injected commit failure (failpoint='commit'): transaction "
                "rolled back, no partial update is visible",
                {"failpoint": "commit"},
            )

        conn.commit()
        return counts

    except ResourceExhaustedError:
        if tx_started:
            conn.rollback()
        raise
    except StateConflictError:
        if tx_started:
            conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if tx_started:
            conn.rollback()
        raise StateConflictError(
            CONSTRAINT_VIOLATION_CODE,
            f"constraint violation during execution: {exc}; transaction rolled back",
            {"sqlite_error": str(exc)},
        )
    except sqlite3.OperationalError as exc:
        if tx_started:
            conn.rollback()
        if _is_locked(exc):
            raise ResourceExhaustedError(
                DB_LOCKED_CODE, f"database locked during merge: {exc}"
            )
        raise ComputationFailureError(
            PREDICATE_FAILED_CODE, f"SQLite operational error: {exc}"
        )
    except sqlite3.DatabaseError as exc:
        if tx_started:
            conn.rollback()
        raise ComputationFailureError(
            PREDICATE_FAILED_CODE, f"unexpected SQLite error: {exc}"
        )


def _is_locked(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _run(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> None:
    try:
        conn.execute(sql, params)
    except sqlite3.OperationalError as exc:
        if _is_locked(exc):
            raise ResourceExhaustedError(
                DB_LOCKED_CODE, f"database locked while applying action: {exc}"
            )
        raise


def _assert_not_stale(
    conn: sqlite3.Connection,
    plan_spec: Any,
    action: Any,
    snapshot: dict[int, dict[str, Any]],
    watch_cols: list[str],
) -> None:
    rowid = action.target_rowid
    quoted = ", ".join(quote_ident(c) for c in watch_cols)
    cur = conn.execute(
        f"SELECT {quoted} FROM {quote_ident(plan_spec.target_table)} WHERE rowid = ?",
        (rowid,),
    )
    record = cur.fetchone()
    if record is None:
        raise StateConflictError(
            SNAPSHOT_STALE_CODE,
            f"target row rowid={rowid} disappeared between snapshot and execution",
            {"target_rowid": rowid, "kind": "row_deleted"},
        )
    current = {c: record[i] for i, c in enumerate(watch_cols)}
    prior = snapshot.get(rowid, {})
    compared = {c: prior.get(c) for c in watch_cols}
    if canonical_json(current) != canonical_json(compared):
        raise StateConflictError(
            SNAPSHOT_STALE_CODE,
            "target row changed between snapshot and execution; aborting on "
            "snapshot isolation rather than overwriting a concurrent commit",
            {
                "target_rowid": rowid,
                "kind": "values_changed",
                "snapshot": compared,
                "current": current,
            },
        )


def _build_update(spec: Any, action: Any) -> tuple[str, tuple[Any, ...]]:
    cols = sorted(action.new_values)
    set_clause = ", ".join(f"{quote_ident(c)} = ?" for c in cols)
    params = tuple(action.new_values[c] for c in cols) + (action.target_rowid,)
    sql = (
        f"UPDATE {quote_ident(spec.target_table)} SET {set_clause} "
        "WHERE rowid = ?"
    )
    return sql, params


def _build_insert(spec: Any, action: Any) -> tuple[str, tuple[Any, ...]]:
    cols = sorted(action.new_values)
    col_clause = ", ".join(quote_ident(c) for c in cols)
    placeholders = ", ".join("?" for _ in cols)
    sql = (
        f"INSERT INTO {quote_ident(spec.target_table)} ({col_clause}) "
        f"VALUES ({placeholders})"
    )
    return sql, tuple(action.new_values[c] for c in cols)
