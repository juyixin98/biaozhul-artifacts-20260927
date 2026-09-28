"""Invariant-focused tests for the two structural MERGE guarantees.

Run ids: TC-INV-*.

A. MATCHED decisions are made exclusively against the pre-operation target
   snapshot - a row inserted earlier in the same batch is invisible to later
   source rows (no "insert then self-match" chaining).
B. Source duplicate rejection is by key grouping, never by row order.
"""

from __future__ import annotations

import pytest

from merge_engine.adapter import load_source_rows
from merge_engine.contract import MergeSpec
from merge_engine.planner import build_plan, prepare
from merge_engine.snapshot import snapshot_target, table_columns

from conftest import records_source


def _build(service, spec_dict, rows):
    spec = MergeSpec.from_payload(spec_dict)
    source_rows, sfp = load_source_rows(records_source(rows), max_rows=spec.max_source_rows)
    cols = table_columns(service.conn, spec.target_table)
    prepared = prepare(
        spec,
        source_columns=sorted({c for r in rows for c in r}),
        target_columns=cols,
        not_null_target_columns=service._not_null_columns(spec.target_table),
    )
    needed = sorted(set(prepared.needed_target_columns()) | set(spec.key_columns))
    target_rows, _, tfp = snapshot_target(service.conn, spec, needed)
    return build_plan(
        prepared, source_rows, target_rows,
        source_fingerprint=sfp, target_fingerprint=tfp,
    )


def test_tc_inv_01_same_batch_insert_is_invisible_to_later_match(service):
    """TC-INV-01 (behavioral): two source rows share a key absent from target.

    If matching could see earlier in-batch inserts, the second row could be
    interpreted as MATCHing the row the first inserts. The contract instead
    rejects source-internal duplicate keys on a grouping that is independent
    of row order - so the ambiguity cannot exist regardless of order.
    """
    spec = {
        "target_table": "no_pk_orders",
        "key_columns": ["region", "id"],
        "update_columns": ["status"],
        "insert_columns": ["status"],
        "null_policy": "NULLS_NOT_DISTINCT",
        "when_clauses": [
            {"matched": True, "action": "update",
             "assignments": {"status": "S.status"}},
            {"matched": False, "action": "insert",
             "assignments": {"status": "S.status"}},
        ],
    }
    rows = [
        {"region": "cn", "id": 10, "status": "first"},
        {"region": "cn", "id": 10, "status": "second"},
    ]
    from merge_engine.errors import SOURCE_DUPLICATE_KEY_CODE, Category

    with pytest.raises(Exception) as ei:
        _build(service, spec, rows)
    err = ei.value
    assert err.category == Category.INPUT_ERROR
    assert err.code == SOURCE_DUPLICATE_KEY_CODE

    # and the conflict is raised identically when the rows are reversed
    with pytest.raises(Exception) as ei2:
        _build(service, spec, list(reversed(rows)))
    assert ei2.value.code == SOURCE_DUPLICATE_KEY_CODE
    assert ei2.value.details["duplicate_groups"][0]["key"] == ["cn", 10]


def test_tc_inv_02_distinct_keys_both_insert_despite_chain_opportunity(service):
    """TC-INV-02: two distinct absent keys are both INSERTs even though a
    naive implementation might INSERT the first then observe it. The second
    key is different, but additionally we verify neither action carries a
    target_rowid (the tell-tale of matching something this batch inserted)."""
    spec = {
        "target_table": "no_pk_orders",
        "key_columns": ["region", "id"],
        "update_columns": ["status"],
        "insert_columns": ["status"],
        "null_policy": "NULLS_NOT_DISTINCT",
        "when_clauses": [
            {"matched": True, "action": "update",
             "assignments": {"status": "S.status"}},
            {"matched": False, "action": "insert",
             "assignments": {"status": "S.status"}},
        ],
    }
    rows = [
        {"region": "cn", "id": 10, "status": "a"},
        {"region": "cn", "id": 11, "status": "b"},
        {"region": "us", "id": 20, "status": "c"},
    ]
    plan = _build(service, spec, rows)
    assert [a.outcome.value for a in plan.actions] == ["INSERT"] * 3
    assert all(a.target_rowid is None for a in plan.actions)
    # and decisions confirm the match was against the empty-for-keys snapshot
    assert all(d.matched is False for d in plan.decisions)


def test_tc_inv_03_planner_only_reads_snapshot_structure():
    """TC-INV-03 (structural): the pure planner is physically incapable of
    issuing SQL - build_plan's globals contain no connection/cursor type and
    its call signature only accepts rows. This guards against a regression
    where planning starts observing execution effects."""
    import inspect

    from merge_engine import planner

    source = inspect.getsource(planner.build_plan)
    # no SQL strings / connection calls may appear in the decision core
    forbidden = ("INSERT INTO", "UPDATE ", "DELETE FROM", "conn.execute",
                 "cursor(", ".commit()", "BEGIN")
    for token in forbidden:
        assert token not in source, f"decision core leaked execution construct: {token}"
    # the core consumes only in-memory rows
    sig = inspect.signature(planner.build_plan)
    params = set(sig.parameters)
    assert {"prepared", "source_rows", "target_rows"} <= params
    assert not any("conn" in p or "cursor" in p for p in params)


def test_tc_inv_04_inserted_key_does_not_redirect_a_later_update(service):
    """TC-INV-04: insert key X in the batch while target *also* contains X
    would be a source-dup conflict, so instead use: source inserts X, later
    source row updates existing Y, and assert the UPDATE targets Y's real
    pre-snapshot rowid - never a rowid that the executor's INSERT created."""
    spec = {
        "target_table": "no_pk_orders",
        "key_columns": ["region", "id"],
        "update_columns": ["status"],
        "insert_columns": ["status"],
        "null_policy": "NULLS_NOT_DISTINCT",
        "when_clauses": [
            {"matched": True, "action": "update",
             "assignments": {"status": "S.status"}},
            {"matched": False, "action": "insert",
             "assignments": {"status": "S.status"}},
        ],
    }
    service.conn.execute(
        "INSERT INTO no_pk_orders(region,id,status) VALUES ('cn',5,'old')"
    )
    service.conn.commit()
    existing_rowid = service.conn.execute(
        "SELECT rowid FROM no_pk_orders WHERE region='cn' AND id=5"
    ).fetchone()[0]

    rows = [
        {"region": "cn", "id": 99, "status": "brand-new"},  # inserts
        {"region": "cn", "id": 5, "status": "refreshed"},   # updates snapshot row
        {"region": "cn", "id": 100, "status": "brand-new2"},
    ]
    # present rows in an order where the insert comes FIRST
    plan = _build(service, spec, rows)
    by_key = {tuple(a.key): a for a in plan.actions}
    update = by_key[("cn", 5)]
    assert update.outcome.value == "UPDATE"
    assert update.target_rowid == existing_rowid
    assert update.new_values["status"] == "refreshed"
    for k in (("cn", 99), ("cn", 100)):
        assert by_key[k].outcome.value == "INSERT"
        assert by_key[k].target_rowid is None

    # and the executor, applying actions in plan order, still yields exactly
    # one row per key (no accidental overwrite of the fresh INSERT).
    from merge_engine.executor import execute_plan

    counts = execute_plan(service.conn, plan)
    assert counts == {"update": 1, "insert": 2, "delete": 0}
    final = {
        (r[0], r[1]): r[2]
        for r in service.conn.execute(
            "SELECT region,id,status FROM no_pk_orders ORDER BY region,id"
        ).fetchall()
    }
    assert final[("cn", 5)] == "refreshed"
    assert final[("cn", 99)] == "brand-new"
    assert final[("cn", 100)] == "brand-new2"


def test_tc_inv_05_match_index_built_once_before_decisions(service):
    """TC-INV-05: matched flags are independent of processing order even when
    inserts sort before updates in the deterministic key order."""
    spec = {
        "target_table": "no_pk_orders",
        "key_columns": ["region", "id"],
        "update_columns": ["status"],
        "insert_columns": ["status"],
        "null_policy": "NULLS_NOT_DISTINCT",
        "when_clauses": [
            {"matched": True, "action": "update",
             "assignments": {"status": "S.status"}},
            {"matched": False, "action": "insert",
             "assignments": {"status": "S.status"}},
        ],
    }
    service.conn.execute(
        "INSERT INTO no_pk_orders(region,id,status) VALUES ('zz',1,'t')"
    )
    service.conn.commit()
    rows = [
        {"region": "zz", "id": 1, "status": "matched-sorts-last"},
        {"region": "aa", "id": 1, "status": "insert-sorts-first"},
    ]
    plan_a = _build(service, spec, rows)
    plan_b = _build(service, spec, list(reversed(rows)))
    view = lambda p: [(tuple(a.key), a.outcome.value, a.target_rowid) for a in p.actions]
    assert view(plan_a) == view(plan_b)
    # deterministic order: insert key aa/1 sorts BEFORE matched update zz/1,
    # yet zz/1 still MATCHED against the snapshot (would be impossible if the
    # insert fed the match index).
    assert view(plan_a) == [
        (("aa", 1), "INSERT", None),
        (("zz", 1), "UPDATE", plan_a.actions[1].target_rowid),
    ]
    assert view(plan_a)[1][2] is not None
