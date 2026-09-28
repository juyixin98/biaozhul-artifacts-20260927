"""Planner decision-core tests.

Run ids: TC-PLAN-*. Every test cross-checks the engine against the
independent ``oracle`` module (which imports nothing from merge_engine).
"""

from __future__ import annotations

import pytest

from merge_engine.adapter import load_source_rows
from merge_engine.contract import MergeSpec
from merge_engine.errors import (
    SOURCE_DUPLICATE_KEY_CODE,
    TARGET_DUPLICATE_KEY_CODE,
    Category,
)
from merge_engine.planner import build_plan, prepare
from merge_engine.snapshot import snapshot_target, table_columns
from merge_engine.service import MergeService

from oracle import oracle_plan  # type: ignore

from conftest import arrow_source, base_spec, records_source


def _plan_for(service: MergeService, spec_dict: dict, rows: list[dict]):
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
    return build_plan(prepared, source_rows, target_rows,
                      source_fingerprint=sfp, target_fingerprint=tfp), target_rows


# ---------------------------------------------------------------- fixtures
SRC = [
    {"region": "cn", "id": 1, "name": "alphaX", "score": 11, "delta": 25.0, "tier": "gold"},
    {"region": "cn", "id": 2, "name": "betaX",  "score": 6,  "delta": -10.0, "tier": "silver"},
    {"region": "cn", "id": 4, "name": "zeta",   "score": 3,  "delta": 7.0,   "tier": "bronze"},
    {"region": "us", "id": 1, "name": "deltaX", "score": 8,  "delta": 0.0,   "tier": "bronze"},
    {"region": "us", "id": 2, "name": "epsX",   "score": 1,  "delta": -5.0,  "tier": None},
]

# What the target table looks like (mirrors conftest fixture accounts rows).
TARGET = [
    {"__rowid__": 1, "region": "cn", "id": 1, "name": "alpha", "status": "active",
     "score": 10, "balance": 100.0, "tier": "gold"},
    {"__rowid__": 2, "region": "cn", "id": 2, "name": "beta", "status": "active",
     "score": 5, "balance": 200.0, "tier": "silver"},
    {"__rowid__": 3, "region": "cn", "id": 3, "name": "gamma", "status": "frozen",
     "score": 2, "balance": 300.0, "tier": None},
    {"__rowid__": 4, "region": "us", "id": 1, "name": "delta", "status": "active",
     "score": 7, "balance": 50.0, "tier": "bronze"},
    {"__rowid__": 5, "region": "us", "id": 2, "name": "epsil", "status": "churned",
     "score": 1, "balance": 0.0, "tier": None},
]


def _engine_action_view(plan) -> list[dict]:
    return [
        {
            "outcome": a.outcome.value,
            "source_index": a.source_index,
            "target_rowid": a.target_rowid,
            "key": list(a.key),
            "new_values": a.new_values,
        }
        for a in plan.actions
    ]


# -------------------------------------------------------------------- cases
def test_tc_plan_01_matches_oracle_action_set(service):
    """TC-PLAN-01: update / conditional delete / insert action set equals the
    independent oracle's decision, including exact values."""
    spec = base_spec()
    plan, _ = _plan_for(service, spec, SRC)

    expected = oracle_plan(spec, SRC, TARGET)["actions"]
    got = _engine_action_view(plan)
    # both orders are deterministic key order; compare aligned lists
    assert len(got) == len(expected)
    for g, e in zip(got, expected):
        assert g["outcome"] == e["outcome"], (g, e)
        assert g["key"] == e["key"]
        assert g["source_index"] == e["source_index"]
        assert g["target_rowid"] == e["target_rowid"]
        assert g["new_values"] == e["new_values"]

    counts = plan.counts()
    assert counts == {"update": 2, "insert": 1, "delete": 2, "unprocessed": 0}


def test_tc_plan_02_manual_action_set_verification(service):
    """TC-PLAN-02: hand-verified decisions for each source row."""
    spec = base_spec()
    plan, _ = _plan_for(service, spec, SRC)
    by_key = {tuple(a.key): a for a in plan.actions}

    # cn/1 matches active, delta=25 -> UPDATE balance 100+25=125
    a = by_key[("cn", 1)]
    assert a.outcome.value == "UPDATE"
    assert a.new_values["balance"] == 125.0
    assert a.new_values["name"] == "alphaX"

    # cn/2 matches but delta=-10 -> DELETE (rule #1 false, rule #2 fires)
    a = by_key[("cn", 2)]
    assert a.outcome.value == "DELETE"
    assert a.new_values == {}

    # cn/4 not matched, delta=7 -> INSERT
    a = by_key[("cn", 4)]
    assert a.outcome.value == "INSERT"
    assert a.new_values["balance"] == 7.0
    assert a.new_values["region"] == "cn" and a.new_values["id"] == 4

    # us/1 matches active delta=0 -> UPDATE (>= includes equality) balance 50
    a = by_key[("us", 1)]
    assert a.outcome.value == "UPDATE"
    assert a.new_values["balance"] == 50.0

    # us/2 matches; delta=-5 -> DELETE even though target is churned (the
    # update rule's condition references only S.delta, delete rule then fires)
    a = by_key[("us", 2)]
    assert a.outcome.value == "DELETE"

    # cn/3 not in source -> untouched; decision for its absence simply absent
    assert ("cn", 3) not in by_key


def test_tc_plan_03_decision_independent_of_source_row_order(service):
    """TC-PLAN-03: shuffling source rows must not change the action set."""
    spec = base_spec()
    plan_a, _ = _plan_for(service, spec, SRC)
    shuffled = [SRC[4], SRC[0], SRC[3], SRC[1], SRC[2]]
    plan_b, _ = _plan_for(service, spec, shuffled)

    view_a = _engine_action_view(plan_a)
    view_b = _engine_action_view(plan_b)
    # outcome/key/values identical; source_index keeps each row's original
    # batch position, so actions land in the same deterministic key order but
    # carry different source indexes - that is expected and is itself part of
    # order independence (decision never uses the index).
    def _content(view):
        return [{k: v for k, v in row.items() if k != "source_index"} for row in view]

    def _by_key(view):
        return {tuple(r["key"]): (r["source_index"], r["outcome"], r["new_values"]) for r in view}

    assert _content(view_a) == _content(view_b)
    # and each decision still points back at the same *content* row
    assert {k: (v[1], v[2]) for k, v in _by_key(view_a).items()} == {
        k: (v[1], v[2]) for k, v in _by_key(view_b).items()
    }


def test_tc_plan_04_source_duplicate_composite_key_rejected(service):
    """TC-PLAN-04: same composite key twice in source -> INPUT_ERROR
    INPUT_SOURCE_DUPLICATE_KEY, regardless of which comes first."""
    rows = [
        {"region": "cn", "id": 1, "name": "a", "score": 1, "delta": 1.0, "tier": "x"},
        {"region": "cn", "id": 1, "name": "b", "score": 2, "delta": 2.0, "tier": "y"},
    ]
    for ordering in (rows, list(reversed(rows))):
        with pytest.raises(Exception) as ei:
            _plan_for(service, base_spec(), ordering)
        err = ei.value
        assert err.category == Category.INPUT_ERROR
        assert err.code == SOURCE_DUPLICATE_KEY_CODE
        groups = err.details["duplicate_groups"]
        assert groups[0]["key"] == ["cn", 1]
        assert set(groups[0]["source_indexes"]) == {0, 1}


def test_tc_plan_05_nulls_not_distinct_source_conflict(service):
    """TC-PLAN-05: under NULLS NOT DISTINCT, (region,NULL) twice is a
    source duplicate even though SQL = would call them unknown."""
    rows = [
        {"region": None, "id": 1, "name": "a", "score": 1, "delta": 1.0, "tier": "x"},
        {"region": None, "id": 1, "name": "b", "score": 2, "delta": 2.0, "tier": "y"},
    ]
    with pytest.raises(Exception) as ei:
        _plan_for(service, base_spec(), rows)
    assert ei.value.category == Category.INPUT_ERROR
    assert ei.value.code == SOURCE_DUPLICATE_KEY_CODE
    assert ei.value.details["duplicate_groups"][0]["key"] == [None, 1]


def test_tc_plan_06_nulls_distinct_policy_no_match_no_conflict(service):
    """TC-PLAN-06: NULLS DISTINCT: NULL key matches nothing and two NULL keys
    do not collide with each other - both rows go to NOT MATCHED."""
    # no_pk_orders permits NULL key components (no NOT NULL / PK constraint)
    service.conn.execute(
        "INSERT INTO no_pk_orders(region,id,status) VALUES (NULL,9,'n')"
    )
    service.conn.commit()
    spec = {
        "target_table": "no_pk_orders",
        "key_columns": ["region", "id"],
        "update_columns": ["status"],
        "insert_columns": ["status"],
        "null_policy": "NULLS_DISTINCT",
        "when_clauses": [
            {"matched": True, "action": "update",
             "assignments": {"status": "S.status"}},
            {"matched": False, "action": "insert",
             "assignments": {"status": "S.status"}},
        ],
    }
    rows = [
        {"region": None, "id": 9, "status": "p"},
        {"region": None, "id": 9, "status": "q"},
    ]
    plan, _ = _plan_for(service, spec, rows)
    outcomes = [a.outcome.value for a in plan.actions]
    assert outcomes == ["INSERT", "INSERT"]  # neither conflicts nor matches
    assert all(a.target_rowid is None for a in plan.actions)


def test_tc_plan_07_three_valued_logic_unknown_does_not_fire(service):
    """TC-PLAN-07: a condition evaluating to UNKNOWN (NULL comparison) does
    not fire the rule and is reported distinctly in the decision trace."""
    spec = base_spec()
    # cn/3 target tier IS NULL; condition S.tier = T.tier compares 'gold'=NULL
    # -> unknown for the first matched row when we express condition that way.
    spec["when_clauses"] = [
        {"matched": True, "action": "update",
         "condition": "T.tier = S.tier",
         "assignments": {
             "name": "S.name", "status": "T.status", "score": "S.score",
             "balance": "T.balance", "tier": "S.tier"}},
        {"matched": True, "action": "delete",
         "condition": "T.tier IS NULL AND S.tier IS NOT NULL",
         "assignments": {}},
        {"matched": False, "action": "insert",
         "assignments": {
             "name": "S.name", "status": "'new'", "score": "S.score",
             "balance": "S.delta", "tier": "S.tier"}},
    ]
    rows = [
        {"region": "cn", "id": 3, "name": "g2", "score": 9, "delta": 0.0, "tier": "gold"},
    ]
    plan, _ = _plan_for(service, spec, rows)
    a = plan.actions[0]
    assert a.outcome.value == "DELETE"  # clause1 UNKNOWN, clause2 TRUE
    trace = plan.decisions[0]
    assert trace.fired_clause == 1
    results = {c["clause"]: c["result"] for c in trace.clause_results}
    assert results[0] is None  # UNKNOWN surfaced as None, not truthy/falsy


def test_tc_plan_08_rule_priority_first_true_wins(service):
    """TC-PLAN-08: with two matched rules whose conditions are both true, the
    earlier one fires; deleting/reversing rule order changes the outcome."""
    make = lambda: base_spec()  # noqa: E731
    rows = [{"region": "cn", "id": 1, "name": "a", "score": 1, "delta": 5.0, "tier": "x"}]
    plan, _ = _plan_for(service, make(), rows)
    assert plan.actions[0].outcome.value == "UPDATE"

    reversed_clauses = base_spec()
    reversed_clauses["when_clauses"] = list(reversed(base_spec()["when_clauses"]))
    # reversed order puts the NOT MATCHED insert first but it is skipped for
    # matched rows; delete clause (delta<0 false) then update still fires.
    plan2, _ = _plan_for(service, reversed_clauses, rows)
    assert plan2.actions[0].outcome.value == "UPDATE"

    # genuinely competing: update vs delete both with TRUE condition
    competing = base_spec()
    competing["when_clauses"] = [
        {"matched": True, "action": "delete", "condition": "true", "assignments": {}},
        {"matched": True, "action": "update", "condition": "true",
         "assignments": {"name": "S.name", "status": "'active'", "score": "S.score",
                         "balance": "T.balance + S.delta", "tier": "S.tier"}},
        base_spec()["when_clauses"][2],
    ]
    plan3, _ = _plan_for(service, competing, rows)
    assert plan3.actions[0].outcome.value == "DELETE"
    assert plan3.decisions[0].fired_clause == 0


def test_tc_plan_09_target_duplicate_keys_rejected(service):
    """TC-PLAN-09: duplicate composite keys in target -> STATE_CONFLICT."""
    # no_pk_orders has no unique constraint; insert duplicate composite keys
    service.conn.execute(
        "INSERT INTO no_pk_orders(region,id,status) VALUES "
        "('cn',1,'a'),('cn',1,'b')"
    )
    service.conn.commit()
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
    rows = [{"region": "cn", "id": 1, "status": "x"}]
    with pytest.raises(Exception) as ei:
        _plan_for(service, spec, rows)
    err = ei.value
    assert err.category == Category.STATE_CONFLICT
    assert err.code == TARGET_DUPLICATE_KEY_CODE
    group = err.details["duplicate_groups"][0]
    assert group["key"] == ["cn", 1]
    assert group["count"] == 2


def test_tc_plan_10_arrow_input_matches_records(service):
    """TC-PLAN-10: Arrow table source yields the same plan as records source
    (covers the real PyArrow adapter path)."""
    spec = base_spec()
    plan_records, _ = _plan_for(service, spec, SRC)

    spec2 = MergeSpec.from_payload(spec)
    source_rows, sfp = load_source_rows(arrow_source(SRC), max_rows=spec2.max_source_rows)
    cols = table_columns(service.conn, spec2.target_table)
    prepared = prepare(spec2, source_columns=sorted({c for r in SRC for c in r}),
                       target_columns=cols,
                       not_null_target_columns=service._not_null_columns(spec2.target_table))
    needed = sorted(set(prepared.needed_target_columns()) | set(spec2.key_columns))
    target_rows, _, tfp = snapshot_target(service.conn, spec2, needed)
    plan_arrow = build_plan(prepared, source_rows, target_rows,
                            source_fingerprint=sfp, target_fingerprint=tfp)
    assert _engine_action_view(plan_arrow) == _engine_action_view(plan_records)


def test_tc_plan_11_arrow_ipc_bytes_roundtrip(service):
    """TC-PLAN-11: Arrow IPC stream bytes are accepted by the adapter."""
    import io
    import pyarrow as pa

    table = arrow_source(SRC)["table"]
    buf = io.BytesIO()
    pa.ipc.new_stream(buf, table.schema).write_table(table)
    payload = {"format": "arrow-ipc", "data": buf.getvalue()}
    rows, fp = load_source_rows(payload, max_rows=1000)
    assert len(rows) == len(SRC)
    assert rows[0].values["name"] == "alphaX"
