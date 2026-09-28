"""End-to-end HTTP tests through the FastAPI validation interface."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.container import Container
from app.kernel.errors import ErrorCategory

ROWS = [
    (1, "us", "2024-01-01", 10, "alice@example.test"),
    (2, "us", "2024-01-01", 20, "bob@example.test"),
    (3, "eu", "2024-01-01", 30, "carol@example.test"),
]


@pytest.fixture()
def client(container: Container) -> TestClient:
    app = create_app(container)
    # lifespan runs recover_pending with TestClient context manager.
    with TestClient(app) as c:
        yield c


def test_full_http_flow_table_commit_snapshot(client, make_file):
    r = client.post(
        "/tables", json={"table": "events", "partition_spec": ["region", "day"]}
    )
    assert r.status_code == 201
    assert r.json()["root_snapshot_id"] == 1

    f = make_file(ROWS, name="rows.parquet")
    r = client.post(
        "/commits",
        json={
            "table": "events",
            "operation": "APPEND",
            "request_id": "req-http-1",
            "base_snapshot_id": 1,
            "files": [str(f)],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "COMMITTED"
    assert body["snapshot_id"] == 2
    assert body["total_row_count"] == 3
    assert sorted(body["partition_keys"]) == [
        "region=eu/day=2024-01-01",
        "region=us/day=2024-01-01",
    ]

    r = client.get("/tables/events/snapshots/latest")
    assert r.status_code == 200
    snap = r.json()
    assert snap["row_count"] == 3
    assert len(snap["files"]) == 1

    r = client.get("/tables/events/snapshots")
    assert [s["operation"] for s in r.json()["snapshots"]] == ["ROOT", "APPEND"]


def test_http_conflict_returns_named_category_and_request_id(client, make_file):
    client.post(
        "/tables", json={"table": "events", "partition_spec": ["region", "day"]}
    )
    f1 = make_file(ROWS[:1], name="a.parquet")
    f2 = make_file(ROWS[1:2], name="b.parquet")
    client.post(
        "/commits",
        json={
            "table": "events", "operation": "APPEND", "request_id": "req-a",
            "base_snapshot_id": 1, "files": [str(f1)],
        },
    )
    r = client.post(
        "/commits",
        json={
            "table": "events", "operation": "APPEND", "request_id": "req-b",
            "base_snapshot_id": 1, "files": [str(f2)],
        },
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == ErrorCategory.CONFLICT_OVERLAPPING_PARTITION.value
    assert body["request_id"] == "req-b"
    assert "region=us/day=2024-01-01" in body["details"]["overlapping_partitions"]


def test_http_bad_file_returns_staging_failure(client, make_file):
    client.post(
        "/tables", json={"table": "events", "partition_spec": ["region"]}
    )
    bad = make_file([], raw_bytes=b"not parquet")
    r = client.post(
        "/commits",
        json={
            "table": "events", "operation": "APPEND", "request_id": "req-bad",
            "base_snapshot_id": 1, "files": [str(bad)],
        },
    )
    assert r.status_code == 422
    assert r.json()["error"] == ErrorCategory.STAGING_FAILED.value


def test_http_lost_response_replay_is_idempotent(client, make_file):
    client.post(
        "/tables", json={"table": "events", "partition_spec": ["region", "day"]}
    )
    f = make_file(ROWS[:1], name="a.parquet")
    payload = {
        "table": "events", "operation": "APPEND", "request_id": "req-idem",
        "base_snapshot_id": 1, "files": [str(f)],
    }
    r1 = client.post("/commits", json=payload)
    r2 = client.post("/commits", json=payload)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["snapshot_id"] == r2.json()["snapshot_id"]
    assert r2.json()["replayed"] is True
    commits = client.get("/commits").json()
    assert len(commits) == 1


def test_diagnostics_redact_sensitive_owner_and_carry_request_id(
    client, container, make_file
):
    client.post(
        "/tables", json={"table": "events", "partition_spec": ["region", "day"]}
    )
    f = make_file(ROWS, name="rows.parquet")
    client.post(
        "/commits",
        json={
            "table": "events", "operation": "APPEND", "request_id": "req-secret",
            "base_snapshot_id": 1, "files": [str(f)],
        },
    )
    log_text = container.diagnostics.log_path.read_text(encoding="utf-8")
    lines = [json.loads(line) for line in log_text.splitlines() if line.strip()]
    assert all("req-" in line["request_id"] for line in lines)
    # Raw sensitive email values must never appear.
    assert "alice@example.test" not in log_text
    assert "bob@example.test" not in log_text
    # Decisions and key state are present.
    events = {line["event"]: line for line in lines}
    assert events["commit.committed"]["decision"] == "ACCEPTED"
    assert events["commit.committed"]["state"]["head_snapshot_id"] == 2
