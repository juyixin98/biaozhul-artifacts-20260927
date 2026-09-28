"""Atomicity and error-category tests.

Run ids: TC-ATOM-* / TC-ERR-*. Failure categories under test:

* INPUT_ERROR       - malformed request, schema, row limit
* STATE_CONFLICT    - constraint violation, stale snapshot
* RESOURCE_EXHAUSTED- injected commit failure, (best-effort) SQLite lock
* COMPUTATION_FAILURE - condition/assignment evaluation errors
"""

from __future__ import annotations

import sqlite3

import pytest

from merge_engine import metadata as meta
from merge_engine.errors import (
    COMMIT_FAILED_CODE,
    CONSTRAINT_VIOLATION_CODE,
    PREDICATE_FAILED_CODE,
    ROW_LIMIT_CODE,
    SNAPSHOT_STALE_CODE,
    SOURCE_SCHEMA_CODE,
    SPEC_INVALID_CODE,
    Category,
)
from merge_engine.service import MergeService

from conftest import base_spec, make_request, records_source


def _target_dump(service: MergeService) -> list[tuple]:
    return service.conn.execute(
        "SELECT region,id,name,status,score,balance,tier "
        "FROM accounts ORDER BY region,id"
    ).fetchall()


MERGE_ROWS = [
    {"region": "cn", "id": 1, "name": "a1", "score": 11, "delta": 25.0, "tier": "gold"},
    {"region": "cn", "id": 2, "name": "a2", "score": 6, "delta": -10.0, "tier": "silver"},
    {"region": "cn", "id": 7, "name": "new", "score": 4, "delta": 8.0, "tier": "bronze"},
]


# ------------------------------------------------------------- injected fault
def test_tc_atom_01_commit_failure_leaves_no_partial_update(service, tmp_dir):
    """TC-ATOM-01: injected commit failure must roll back every action.

    The plan contains UPDATE + DELETE + INSERT; after COMMIT_FAILED the
    target table must be byte-identical to its pre-merge content.
    """
    before = _target_dump(service)

    with pytest.raises(Exception) as ei:
        service.merge(
            make_request(base_spec(), records_source(MERGE_ROWS), failpoint_commit=True),
            failpoint="commit",
        )
    err = ei.value
    assert err.category == Category.RESOURCE_EXHAUSTED
    assert err.code == COMMIT_FAILED_CODE
    run_id = err._run_id

    after = _target_dump(service)
    assert after == before, f"partial update visible after rollback: {after}"

    # metadata records the failed run independently of the data rollback
    run = service.get_run(run_id)
    assert run["status"] == "failed"
    assert run["error_category"] == Category.RESOURCE_EXHAUSTED.value
    assert run["error_code"] == COMMIT_FAILED_CODE
    # the planned action set is still present for replay
    actions = service.get_actions(run_id)
    assert {a["outcome"] for a in actions} == {"UPDATE", "DELETE", "INSERT"}


def test_tc_atom_02_validate_changes_nothing(service):
    """TC-ATOM-02: validate returns the plan but the target is untouched."""
    before = _target_dump(service)
    res = service.validate(make_request(base_spec(), records_source(MERGE_ROWS)))
    assert res["status"] == "planned"
    assert _target_dump(service) == before
    run = service.get_run(res["run_id"])
    assert run["status"] == "planned"
    assert run["n_insert"] + run["n_update"] + run["n_delete"] == 3


def test_tc_atom_03_successful_commit_applies_exact_action_set(service):
    """TC-ATOM-03: the committed table equals pre-state with actions applied."""
    res = service.merge(make_request(base_spec(), records_source(MERGE_ROWS)))
    assert res["committed"] is True
    rows = {
        (r[0], r[1]): r for r in _target_dump(service)
    }
    # cn/1 updated (100+25), cn/2 deleted, cn/7 inserted, others untouched
    assert rows[("cn", 1)][5] == 125.0
    assert ("cn", 2) not in rows
    assert rows[("cn", 7)][2] == "new"
    assert rows[("cn", 3)][1] == 3  # untouched
    assert rows[("us", 1)][1] == 1
    assert res["committed_counts"] == {"update": 1, "insert": 1, "delete": 1}


def test_tc_atom_04_constraint_violation_rolls_back(service):
    """TC-ATOM-04: an INSERT violating NOT NULL arrives pre-validated normally;
    force an execution-time constraint violation via a trigger and assert
    STATE_CONFLICT / CONSTRAINT_VIOLATION plus zero partial updates."""
    service.conn.execute(
        "CREATE TRIGGER IF NOT EXISTS accounts_no_gold_insert "
        "BEFORE INSERT ON accounts "
        "WHEN NEW.tier = 'gold' BEGIN "
        "  SELECT RAISE(ABORT, 'synthetic constraint: no gold inserts'); END;"
    )
    service.conn.commit()
    rows = [
        # this insert is rejected by the trigger
        {"region": "eu", "id": 1, "name": "x", "score": 1, "delta": 1.0, "tier": "gold"},
        {"region": "eu", "id": 2, "name": "y", "score": 1, "delta": 1.0, "tier": "bronze"},
    ]
    before = _target_dump(service)
    with pytest.raises(Exception) as ei:
        service.merge(make_request(base_spec(), records_source(rows)))
    err = ei.value
    assert err.category == Category.STATE_CONFLICT
    assert err.code == CONSTRAINT_VIOLATION_CODE
    assert _target_dump(service) == before


def test_tc_atom_05_snapshot_staleness_aborts_before_write(service):
    """TC-ATOM-05: if a concurrent writer commits between planning and the
    execution tx, the staleness guard raises STATE_SNAPSHOT_STALE."""
    # Plan first (service builds plan then executes in one call), so instead
    # drive the executor with a manually stale snapshot by mutating through a
    # second connection after we lock the row externally. We simulate by
    # constructing the plan then mutating before execute_plan.
    from merge_engine.adapter import load_source_rows
    from merge_engine.contract import MergeSpec
    from merge_engine.executor import execute_plan
    from merge_engine.planner import prepare
    from merge_engine.snapshot import snapshot_target, table_columns

    spec = MergeSpec.from_payload(base_spec())
    source_rows, sfp = load_source_rows(records_source(MERGE_ROWS), max_rows=1000)
    cols = table_columns(service.conn, spec.target_table)
    prepared = prepare(spec, source_columns=sorted({c for r in MERGE_ROWS for c in r}),
                       target_columns=cols,
                       not_null_target_columns=service._not_null_columns("accounts"))
    needed = sorted(set(prepared.needed_target_columns()) | set(spec.key_columns))
    target_rows, _, tfp = snapshot_target(service.conn, spec, needed)
    plan = build_plan_quiet(prepared, source_rows, target_rows, sfp, tfp)

    # concurrent writer commits a change to the row the plan will UPDATE
    other = sqlite3.connect(service.db_path)
    other.execute("UPDATE accounts SET balance = 999 WHERE region='cn' AND id=1")
    other.commit()
    other.close()

    with pytest.raises(Exception) as ei:
        execute_plan(service.conn, plan)
    err = ei.value
    assert err.category == Category.STATE_CONFLICT
    assert err.code == SNAPSHOT_STALE_CODE
    details = err.details
    assert details["kind"] == "values_changed"
    assert details["current"]["balance"] == 999.0
    # balance row change itself was the concurrent commit; our tx changed none
    assert service.conn.execute(
        "SELECT name FROM accounts WHERE region='cn' AND id=1"
    ).fetchone()[0] == "alpha"


def build_plan_quiet(prepared, source_rows, target_rows, sfp, tfp):
    from merge_engine.planner import build_plan
    return build_plan(prepared, source_rows, target_rows,
                      source_fingerprint=sfp, target_fingerprint=tfp)


def test_tc_atom_06_db_lock_is_resource_exhausted(service, tmp_dir):
    """TC-ATOM-06: a write lock held by another connection maps to
    RESOURCE_EXHAUSTED / DB_LOCKED (best-effort timing test, short timeout)."""
    from merge_engine.service import connect

    # Initialize the second service (its metadata DDL writes) BEFORE the
    # holder takes the lock; the lock must be struck during the merge tx.
    blocked = MergeService(service.db_path,
                           log_path=f"{tmp_dir}/logs/blocked.jsonl", timeout=0.1)
    blocked.conn.execute("PRAGMA busy_timeout = 100")
    holder = connect(service.db_path, timeout=0.1)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE accounts SET score = score WHERE rowid = 1")
    try:
        with pytest.raises(Exception) as ei:
            blocked.merge(make_request(base_spec(), records_source(MERGE_ROWS)))
        err = ei.value
        assert err.category == Category.RESOURCE_EXHAUSTED
        assert err.code == "RESOURCE_DB_LOCKED"
    finally:
        holder.rollback()
        holder.close()
        blocked.close()


# --------------------------------------------------------------- input errors
def test_tc_err_01_bad_spec_category_input(service):
    """TC-ERR-01: malformed spec -> INPUT_ERROR / INPUT_INVALID_SPEC."""
    bad = make_request(
        {"target_table": "accounts", "key_columns": [], "when_clauses": []},
        records_source([]),
    )
    with pytest.raises(Exception) as ei:
        service.merge(bad)
    err = ei.value
    assert err.category == Category.INPUT_ERROR
    assert err.code == SPEC_INVALID_CODE
    # rejected run recorded even before plan existed
    run = service.get_run(err._run_id)
    assert run["status"] == "rejected"
    assert run["error_category"] == Category.INPUT_ERROR.value


def test_tc_err_02_bad_source_shape(service):
    """TC-ERR-02: unknown source format -> INPUT_ERROR / INPUT_SOURCE_SCHEMA."""
    req = make_request(base_spec(), {"format": "parquet", "records": []})
    with pytest.raises(Exception) as ei:
        service.merge(req)
    assert ei.value.category == Category.INPUT_ERROR
    assert ei.value.code == SOURCE_SCHEMA_CODE


def test_tc_err_03_row_limit_is_input_resource_cap(service):
    """TC-ERR-03: exceeding max_source_rows -> INPUT_ERROR / ROW_LIMIT_EXCEEDED."""
    spec = base_spec(max_source_rows=2)
    with pytest.raises(Exception) as ei:
        service.merge(make_request(spec, records_source(MERGE_ROWS)))
    assert ei.value.category == Category.INPUT_ERROR
    assert ei.value.code == ROW_LIMIT_CODE
    assert ei.value.details["source_rows"] == 3
    assert ei.value.details["max_source_rows"] == 2


def test_tc_err_04_computation_failure_category(service):
    """TC-ERR-04: division by zero in an assignment -> COMPUTATION_FAILURE and
    no data change (all expressions validate before any SQL executes)."""
    spec = base_spec()
    spec["when_clauses"][0]["assignments"]["score"] = "1 / 0"
    before = _target_dump(service)
    with pytest.raises(Exception) as ei:
        service.merge(make_request(spec, records_source(MERGE_ROWS)))
    err = ei.value
    assert err.category == Category.COMPUTATION_FAILURE
    assert err.code == PREDICATE_FAILED_CODE
    first = err.details["errors"][0]
    assert "division by zero" in first["error"]
    assert _target_dump(service) == before


def test_tc_err_05_type_error_in_condition_is_computation_failure(service):
    """TC-ERR-05: comparing incompatible runtime types in a condition ->
    COMPUTATION_FAILURE, distinguished from INPUT_ERROR."""
    spec = base_spec()
    spec["when_clauses"][0]["condition"] = "S.name > 5 AND S.delta >= 0"
    with pytest.raises(Exception) as ei:
        service.merge(
            make_request(spec, records_source([
                {"region": "cn", "id": 1, "name": "alpha", "score": 1,
                 "delta": 1.0, "tier": "x"},
            ]))
        )
    assert ei.value.category == Category.COMPUTATION_FAILURE
    assert ei.value.code == PREDICATE_FAILED_CODE


def test_tc_err_06_unknown_column_in_spec_is_input_error(service):
    """TC-ERR-06: expression referencing an unknown column fails at compile
    time -> INPUT_ERROR (the request, not the data, is wrong)."""
    spec = base_spec()
    spec["when_clauses"][0]["condition"] = "S.nonexistent > 1"
    with pytest.raises(Exception) as ei:
        service.merge(make_request(spec, records_source(MERGE_ROWS)))
    assert ei.value.category == Category.INPUT_ERROR
    assert ei.value.code == SPEC_INVALID_CODE


def test_tc_err_07_disallowed_expression_syntax_rejected(service):
    """TC-ERR-07: attribute access / dunder / function outside whitelist is
    refused at compile time (no eval path exists)."""
    spec = base_spec()
    spec["when_clauses"][0]["condition"] = "__import__('os') IS NULL"
    with pytest.raises(Exception) as ei:
        service.merge(make_request(spec, records_source(MERGE_ROWS)))
    assert ei.value.category == Category.INPUT_ERROR
