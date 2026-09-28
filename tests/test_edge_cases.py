"""Edge-case behavior tests.

Run ids: TC-EDGE-*. Empty batch, one-sided rule sets, explicit NULL handling.
"""

from __future__ import annotations

import pytest

from merge_engine.errors import (
    SPEC_INVALID_CODE,
    TARGET_TABLE_MISSING_CODE,
    Category,
)

from conftest import base_spec, make_request, records_source


def test_tc_edge_01_empty_source_is_valid_noop(service):
    """TC-EDGE-01: an empty batch validates to an empty plan and merges
    harmlessly (0 actions), status committed."""
    res = service.merge(make_request(base_spec(), records_source([])))
    assert res["committed_counts"] == {"update": 0, "insert": 0, "delete": 0}
    assert res["actions"] == []
    run = service.get_run(res["run_id"])
    assert run["status"] == "committed"
    assert run["source_rows"] == 0


def test_tc_edge_02_only_matched_clause_unmatched_rows_unprocessed(service):
    """TC-EDGE-02: with no NOT MATCHED rule, unmatched source rows are
    reported UNPROCESSED and no insert occurs."""
    spec = base_spec()
    spec["when_clauses"] = [spec["when_clauses"][0]]  # only the UPDATE rule
    rows = [
        {"region": "cn", "id": 40, "name": "new", "score": 1, "delta": 5.0, "tier": "x"},
    ]
    res = service.validate(make_request(spec, records_source(rows)))
    assert res["actions"] == []
    decision = res["decisions"][0]
    assert decision["outcome"] == "UNPROCESSED"
    assert decision["matched"] is False
    assert "no NOT_MATCHED WHEN rule" in decision["reason"]


def test_tc_edge_03_only_not_matched_clause_matched_rows_unprocessed(service):
    """TC-EDGE-03: with only an INSERT rule, a matched source row is
    UNPROCESSED (never updated/deleted)."""
    spec = base_spec()
    spec["when_clauses"] = [spec["when_clauses"][2]]  # only the INSERT rule
    rows = [
        {"region": "cn", "id": 1, "name": "x", "score": 1, "delta": 5.0, "tier": "x"},
    ]
    before = service.conn.execute(
        "SELECT balance FROM accounts WHERE region='cn' AND id=1"
    ).fetchone()[0]
    res = service.merge(make_request(spec, records_source(rows)))
    assert res["committed_counts"] == {"update": 0, "insert": 0, "delete": 0}
    after = service.conn.execute(
        "SELECT balance FROM accounts WHERE region='cn' AND id=1"
    ).fetchone()[0]
    assert before == after == 100.0


def test_tc_edge_04_ambiguous_bare_column_rejected(service):
    """TC-EDGE-04: a bare column present on both source and target must be
    qualified S./T. rather than silently choosing a side."""
    spec = base_spec()
    # 'score' exists in both fixture source and target
    spec["when_clauses"][0]["condition"] = "score >= 0 AND S.delta >= 0"
    with pytest.raises(Exception) as ei:
        service.merge(make_request(spec, records_source([
            {"region": "cn", "id": 1, "name": "a", "score": 1, "delta": 1.0, "tier": "x"},
        ])))
    err = ei.value
    assert err.category == Category.INPUT_ERROR
    assert err.code == SPEC_INVALID_CODE
    assert "both source and target" in err.message


def test_tc_edge_05_target_reference_in_not_matched_clause_rejected(service):
    """TC-EDGE-05: T.* in a NOT MATCHED clause is an input error."""
    spec = base_spec()
    spec["when_clauses"][2]["condition"] = "S.delta >= 0 AND T.status = 'active'"
    with pytest.raises(Exception) as ei:
        service.merge(make_request(spec, records_source([
            {"region": "cn", "id": 41, "name": "a", "score": 1, "delta": 1.0, "tier": "x"},
        ])))
    assert ei.value.category == Category.INPUT_ERROR
    assert "NOT MATCHED" in ei.value.message


def test_tc_edge_06_unknown_target_table(service):
    """TC-EDGE-06: merging into a missing table is a STATE_CONFLICT."""
    from merge_engine.errors import TARGET_TABLE_MISSING_CODE

    spec = base_spec(target_table="does_not_exist")
    with pytest.raises(Exception) as ei:
        service.merge(make_request(spec, records_source([])))
    assert ei.value.category == Category.STATE_CONFLICT
    assert ei.value.code == TARGET_TABLE_MISSING_CODE
