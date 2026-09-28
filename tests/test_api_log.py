"""HTTP boundary tests (FastAPI + httpx in-process) and replay/metadata tests.

Run ids: TC-API-* / TC-LOG-*.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from merge_engine.api import create_app
from merge_engine.errors import (
    COMMIT_FAILED_CODE,
    SOURCE_DUPLICATE_KEY_CODE,
    TARGET_DUPLICATE_KEY_CODE,
    Category,
)

from conftest import base_spec, make_request, records_source


@pytest.fixture()
def client(service):
    return TestClient(create_app(service))


ROWS = [
    {"region": "cn", "id": 1, "name": "a1", "score": 11, "delta": 25.0, "tier": "gold"},
    {"region": "cn", "id": 2, "name": "a2", "score": 6, "delta": -10.0, "tier": "silver"},
    {"region": "cn", "id": 7, "name": "new", "score": 4, "delta": 8.0, "tier": "bronze"},
]


# ------------------------------------------------------------------ HTTP
def test_tc_api_01_validate_returns_exact_plan(client):
    """TC-API-01: validate reports precise actions, status 200, no side effect."""
    resp = client.post("/v1/merge/validate", json=make_request(base_spec(), records_source(ROWS)))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "planned"
    actions = {(a["outcome"], tuple(a["key"])) for a in body["actions"]}
    assert actions == {
        ("UPDATE", ("cn", 1)),
        ("DELETE", ("cn", 2)),
        ("INSERT", ("cn", 7)),
    }
    # decisions carry the rule trace and reason
    d0 = body["decisions"][0]
    assert "clause_results" in d0 and d0["reason"]


def test_tc_api_02_commit_roundtrip_and_state_check(client, service):
    """TC-API-02: merge commits; target state reflects exact outcome."""
    resp = client.post("/v1/merge", json=make_request(base_spec(), records_source(ROWS)))
    assert resp.status_code == 200
    body = resp.json()
    assert body["committed"] is True
    assert body["committed_counts"] == {"update": 1, "insert": 1, "delete": 1}

    rows = service.conn.execute(
        "SELECT region,id,balance FROM accounts ORDER BY region,id"
    ).fetchall()
    assert ("cn", 1, 125.0) in rows
    assert not any(r[0] == "cn" and r[1] == 2 for r in rows)
    assert ("cn", 7, 8.0) in rows


def test_tc_api_03_error_envelope_categories_and_status(client):
    """TC-API-03: error category maps to the documented HTTP status and the
    envelope carries category + code + details + run_id."""
    # source duplicate -> 400 INPUT_ERROR
    dup = ROWS + [ROWS[0]]
    r1 = client.post("/v1/merge", json=make_request(base_spec(), records_source(dup)))
    assert r1.status_code == 400
    e1 = r1.json()["error"]
    assert e1["category"] == Category.INPUT_ERROR.value
    assert e1["code"] == SOURCE_DUPLICATE_KEY_CODE
    assert "run_id" in r1.json()

    # target duplicate -> 409 STATE_CONFLICT
    service = client.app.state.service
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
            {"matched": True, "action": "update", "assignments": {"status": "S.status"}},
            {"matched": False, "action": "insert", "assignments": {"status": "S.status"}},
        ],
    }
    r2 = client.post(
        "/v1/merge",
        json=make_request(spec, records_source([{"region": "cn", "id": 1, "status": "x"}])),
    )
    assert r2.status_code == 409
    e2 = r2.json()["error"]
    assert e2["category"] == Category.STATE_CONFLICT.value
    assert e2["code"] == TARGET_DUPLICATE_KEY_CODE

    # injected commit failure -> 503 RESOURCE_EXHAUSTED
    r3 = client.post(
        "/v1/merge",
        json=make_request(base_spec(), records_source(ROWS), failpoint_commit=True),
    )
    assert r3.status_code == 503
    e3 = r3.json()["error"]
    assert e3["category"] == Category.RESOURCE_EXHAUSTED.value
    assert e3["code"] == COMMIT_FAILED_CODE

    # computation failure -> 422
    spec4 = base_spec()
    spec4["when_clauses"][0]["assignments"]["score"] = "1 / 0"
    r4 = client.post("/v1/merge", json=make_request(spec4, records_source(ROWS)))
    assert r4.status_code == 422
    assert r4.json()["error"]["category"] == Category.COMPUTATION_FAILURE.value


def test_tc_api_04_run_lookup_endpoints(client):
    """TC-API-04: runs / actions / traces / snapshot are retrievable and the
    snapshot contains enough to replay the decision (source + target)."""
    body = client.post("/v1/merge", json=make_request(base_spec(), records_source(ROWS))).json()
    run_id = body["run_id"]

    listing = client.get("/v1/runs").json()["runs"]
    assert any(r["run_id"] == run_id for r in listing)

    one = client.get(f"/v1/runs/{run_id}").json()
    assert one["status"] == "committed"
    assert one["n_update"] == 1 and one["n_delete"] == 1 and one["n_insert"] == 1

    actions = client.get(f"/v1/runs/{run_id}/actions").json()["actions"]
    assert len(actions) == 3
    traces = client.get(f"/v1/runs/{run_id}/traces").json()["traces"]
    assert {t["source_index"] for t in traces} == {0, 1, 2}

    snap = client.get(f"/v1/runs/{run_id}/snapshot").json()
    assert len(snap["source"]) == 3
    assert {("cn", 1), ("cn", 2), ("us", 2)} <= {
        (r["region"], r["id"]) for r in snap["target"]
    }

    assert client.get("/v1/runs/NO-SUCH-RUN").status_code == 404
    assert client.get("/healthz").json() == {"status": "ok"}


def test_tc_api_05_validate_then_merge_are_separate_runs(client):
    """TC-API-05: a validate and a merge each allocate their own run id and the
    validate leaves status 'planned' while merge ends 'committed'."""
    v = client.post("/v1/merge/validate", json=make_request(base_spec(), records_source(ROWS))).json()
    m = client.post("/v1/merge", json=make_request(base_spec(), records_source(ROWS))).json()
    assert v["run_id"] != m["run_id"]
    assert client.get(f"/v1/runs/{v['run_id']}").json()["status"] == "planned"
    assert client.get(f"/v1/runs/{m['run_id']}").json()["status"] == "committed"


# ------------------------------------------------------------- JSONL replay
def _read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_tc_log_01_run_id_monotonic_and_present_on_every_event(service, tmp_dir):
    """TC-LOG-01: each event line carries run id + monotonic run seq."""
    service.merge(make_request(base_spec(), records_source(ROWS[:2])))
    service.merge(make_request(base_spec(), records_source(ROWS)))
    path = os.path.join(tmp_dir, "logs", "merge-runs.jsonl")
    events = _read_jsonl(path)
    run_ids = {e["run_id"] for e in events if "run_id" in e}
    assert len(run_ids) == 2
    seqs = sorted({e["run_seq"] for e in events if "run_seq" in e})
    assert seqs == [1, 2] or set(seqs) >= {1, 2}
    # seq 1 events precede seq 2 events
    first_seq_events = [e for e in events if e.get("run_seq") == 1]
    second_seq_events = [e for e in events if e.get("run_seq") == 2]
    assert events.index(first_seq_events[0]) < events.index(second_seq_events[0])


def test_tc_log_02_replay_bundle_records_intermediate_states(service, tmp_dir):
    """TC-LOG-02: the JSONL log contains source input, target snapshot with
    duplicate scan, the PLAN action set and the terminal event - enough to
    replay the decision manually."""
    service.merge(make_request(base_spec(), records_source(ROWS)))
    path = os.path.join(tmp_dir, "logs", "merge-runs.jsonl")
    events = _read_jsonl(path)
    kinds = [e["event"] for e in events]
    assert "RUN_START" in kinds
    assert "SNAPSHOT" in kinds
    assert "PLAN" in kinds
    assert "RUN_COMMIT" in kinds

    snap = next(e for e in events if e["event"] == "SNAPSHOT")
    assert snap["target_fingerprint"]
    assert snap["target_duplicate_groups"] == []
    plan_ev = next(e for e in events if e["event"] == "PLAN")
    assert plan_ev["counts"] == {"update": 1, "insert": 1, "delete": 1, "unprocessed": 0}

    # manual replay: the snapshot target row cn/1 balance 100 + source delta 25
    src = next(e for e in events if e["event"] == "RUN_START")["source_rows"]
    cn1_source = next(r for r in src if r["region"] == "cn" and r["id"] == 1)
    cn1_target = next(r for r in snap["target_rows"] if r["region"] == "cn" and r["id"] == 1)
    assert cn1_target["balance"] + cn1_source["delta"] == 125.0
    update_action = next(
        a for a in plan_ev["actions"]
        if a["outcome"] == "UPDATE" and a["key"] == ["cn", 1]
    )
    assert update_action["new_values"]["balance"] == 125.0


def test_tc_log_03_failed_commit_logs_fault_and_rollback_reason(service, tmp_dir):
    """TC-LOG-03: injected commit failure emits COMMIT_FAULT + RUN_FAIL with
    category/code, and the DB audit trail agrees."""
    with pytest.raises(Exception):
        service.merge(
            make_request(base_spec(), records_source(ROWS), failpoint_commit=True),
            failpoint="commit",
        )
    events = _read_jsonl(os.path.join(tmp_dir, "logs", "merge-runs.jsonl"))
    fault = next(e for e in events if e["event"] == "COMMIT_FAULT")
    assert fault["fault"] == "commit"
    fail = next(e for e in events if e["event"] == "RUN_FAIL")
    assert fail["category"] == Category.RESOURCE_EXHAUSTED.value
    assert fail["code"] == COMMIT_FAILED_CODE
    assert fail["stage"] == "execute"

    runs = service.list_runs()
    failed = next(r for r in runs if r["run_id"] == fail["run_id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == COMMIT_FAILED_CODE


def test_tc_log_04_rejected_run_has_complete_audit_trail(client, service, tmp_dir):
    """TC-LOG-04: a request rejected for source duplicates still gets run
    metadata + a RUN_REJECT log line with the exact duplicate group."""
    dup = ROWS + [ROWS[0]]
    resp = client.post("/v1/merge", json=make_request(base_spec(), records_source(dup)))
    assert resp.status_code == 400
    run_id = resp.json()["run_id"]
    run = service.get_run(run_id)
    assert run["status"] == "rejected"
    assert run["error_code"] == SOURCE_DUPLICATE_KEY_CODE

    events = _read_jsonl(os.path.join(tmp_dir, "logs", "merge-runs.jsonl"))
    reject = next(e for e in events if e["event"] == "RUN_REJECT" and e["run_id"] == run_id)
    assert reject["stage"] in {"planner", "spec"}
    assert reject["details"]["duplicate_groups"][0]["key"] == ["cn", 1]


def test_tc_log_05_metadata_run_seq_is_stable_identifier(service):
    """TC-LOG-05: run_seq is a per-database monotonic integer usable for
    ordering; run ids are unique."""
    r1 = service.validate(make_request(base_spec(), records_source(ROWS[:1])))
    r2 = service.validate(make_request(base_spec(), records_source(ROWS[1:2])))
    assert r2["run_seq"] == r1["run_seq"] + 1
    assert r1["run_id"] != r2["run_id"]
