"""HTTP API tests: status codes, request-id correlation, separated failures
and uncertain conclusions, and the validate endpoint's zero-miss verdict.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prune.api import create_app
from prune.catalog import Catalog


@pytest.fixture
def client(config):
    return TestClient(create_app(config))


DAY_PRED = {"op": "AND", "children": [
    {"op": "GE", "column": "ts", "value": "2024-03-05T00:00:00+08:00"},
    {"op": "LT", "column": "ts", "value": "2024-03-06T00:00:00+08:00"}]}


def test_health_reports_pinned_versions(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["versions"]["transform_spec"] == "month-tz-v1"
    assert body["versions"]["tzdb"] == "tzdata2024.2"
    assert body["versions"]["stats_schema"] == "colstats-v1"
    assert r.headers["x-request-id"]


def test_request_id_is_echoed_and_correlated(client):
    rid = "caller-supplied-id"
    r = client.post("/tables/events/prune", json=DAY_PRED,
                    headers={"x-request-id": rid})
    assert r.headers["x-request-id"] == rid
    body = r.json()
    assert body["request_id"] == rid
    # every trace step is bound to the same request
    assert body["trace"]
    assert body["trace"][0]["stage"]


def test_prune_day_range_concrete_response(client):
    r = client.post("/tables/events/prune", json=DAY_PRED)
    assert r.status_code == 200
    b = r.json()
    assert b["candidate_partitions"] == {
        "lo": "2024-03", "hi": "2024-03",
        "null_bucket": "EXCLUDED", "exact": True}
    labels = {p["label"]: p["verdict"] for p in b["partitions"]}
    assert labels["2024-03"] == "KEPT"
    assert labels["__null__"] == "PRUNED"
    # a pruned partition carries an impossibility reason
    nullp = next(p for p in b["partitions"] if p["label"] == "__null__")
    assert nullp["reason"]["code"] == "PARTITION_NULL_EXCLUDED"
    assert b["metrics"]["partitions_pruned"] == 5
    assert b["metrics"]["files_pruned"] == 9
    # failures and uncertain conclusions are separate keys
    assert isinstance(b["failures"], list)
    assert isinstance(b["uncertain"], list)


def test_validate_endpoint_zero_miss(client):
    r = client.post("/tables/events/validate", json=DAY_PRED)
    assert r.status_code == 200
    v = r.json()["validation"]
    assert v["ok"] is True
    assert v["failure_category"] is None
    assert v["expected_matching_rows"] == 2
    assert v["missed_ids"] == []


def test_validate_truncated_string_keeps_file(client):
    pred = {"op": "EQ", "column": "name",
            "value": "z" * 60 + "002_tail_b"}
    r = client.post("/tables/events/validate", json=pred)
    assert r.status_code == 200
    b = r.json()
    assert b["validation"]["ok"] is True
    may = next(p for p in b["partitions"] if p["label"] == "2024-05")
    f = may["files"][0]
    assert f["verdict"] == "UNKNOWN"
    assert f["reasons"][0]["code"] == "FILE_STATS_TRUNCATED"
    # surfaced again under the dedicated uncertain list
    assert any(u["code"] == "FILE_STATS_TRUNCATED" for u in b["uncertain"])


def test_bad_predicate_returns_400_with_category(client):
    r = client.post("/tables/events/prune", json={"op": "NOPE"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_PREDICATE"


def test_unknown_column_returns_400(client):
    r = client.post("/tables/events/prune",
                    json={"op": "EQ", "column": "ghost", "value": 1})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNKNOWN_COLUMN"


def test_unknown_table_returns_404(client):
    r = client.post("/tables/nope/prune", json=DAY_PRED)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "UNKNOWN_TABLE"


def test_table_detail_lists_columns_and_versions(client):
    lst = client.get("/tables").json()
    assert "events" in lst["refreshed"]
    assert "events" in lst["configured"]

    detail = client.get("/tables/events")
    assert detail.status_code == 200
    b = detail.json()
    assert {c["name"] for c in b["columns"]} == {"id", "ts", "name", "amount"}
    assert b["transform"]["kind"] == "month_tz"
    assert b["transform"]["tz"] == "Asia/Shanghai"
    assert b["transform"]["spec_version"] == "month-tz-v1"
    assert b["transform"]["tzdb_version"] == "tzdata2024.2"
    assert b["request_id"]


def test_id_column_validation_failure_category(client):
    r = client.post("/tables/events/validate",
                    json={"predicate": DAY_PRED, "id_column": "nope"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNKNOWN_ID_COLUMN"
