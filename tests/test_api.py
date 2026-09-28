"""HTTP audit interface tests (ASGI in-process via httpx; no network)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from secretscan.api import create_app
from secretscan.service import ScanService  # noqa: F401 (ensures import wiring)
from secretscan.storage import Store

AWS = "AKIAFAKE000000000001"
GHP = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"


@pytest.fixture()
def client(tmp_path):
    state = tmp_path / "state"
    app = create_app(state, "config/rules.yaml")
    with TestClient(app) as c:
        yield c
    app.state.store.close()


def seed(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "f.txt").write_text(f"{AWS}\n{GHP}\n")
    return root


def test_health_reports_versions(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["rules_version"] == "2026.09.01"
    assert body["classification_version"] == "1.0.0"


def test_scan_envelope_correlates_request_id_and_actor(client, tmp_path):
    root = seed(tmp_path / "snap")
    r = client.post(
        "/projects/scan",
        json={"root": str(root), "note": "api test"},
        headers={"x-request-id": "req-api-7", "x-actor": "bob"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["x-request-id"] == "req-api-7"
    env = r.json()
    assert env["ok"] is True
    assert env["request_id"] == "req-api-7"
    data = env["data"]
    assert data["summary"]["candidates_total"] == 2
    assert data["rules_version"] == "2026.09.01"


def test_full_report_contains_no_raw_secret(client, tmp_path):
    root = seed(tmp_path / "snap2")
    scan = client.post(
        "/projects/scan", json={"root": str(root)},
        headers={"x-request-id": "req-api-8", "x-actor": "bob"},
    ).json()["data"]
    pid, sid = scan["project_id"], scan["scan_id"]

    r = client.get(f"/projects/{pid}/scans/{sid}/report")
    assert r.status_code == 200
    raw = r.text
    assert AWS not in raw
    assert GHP not in raw
    report = r.json()["data"]
    assert report["scope"]["request_id"] == "req-api-8"
    assert report["failures"]["file_errors"] == []
    assert "interpretation" in report
    # Markdown rendering must also be secret-free.
    md = client.get(f"/projects/{pid}/scans/{sid}/report?format=md").text
    assert AWS not in md and GHP not in md
    assert "Secret candidate scan report" in md


def test_baseline_flow_over_api(client, tmp_path):
    root = seed(tmp_path / "snap3")
    scan = client.post("/projects/scan", json={"root": str(root)}).json()["data"]
    pid = scan["project_id"]
    cands = client.get(f"/projects/{pid}/candidates").json()["data"]["candidates"]
    aws = next(c for c in cands if c["rule_id"] == "aws_access_key_id")

    r = client.post(
        f"/projects/{pid}/baseline",
        json={"rule_id": "aws_access_key_id", "fingerprint": aws["fingerprint"],
              "note": "fake", "actor": "carol"},
        headers={"x-request-id": "req-base-1"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["state"] == "baseline_exempt"

    listed = client.get(f"/projects/{pid}/baseline").json()["data"]["exemptions"]
    assert len(listed) == 1 and listed[0]["actor"] == "carol"

    events = client.get(f"/projects/{pid}/audit").json()["data"]["events"]
    actions = {e["action"]: e for e in events}
    assert actions["baseline.accept"]["request_id"] == "req-base-1"


def test_error_envelope_has_stable_code(client):
    r = client.post("/projects/scan", json={"root": "/no/such/directory/xyz"})
    assert r.status_code == 400
    env = r.json()
    assert env["ok"] is False
    assert env["error"]["code"] == "validation_error"
    assert "request_id" in env


def test_not_found_scan_returns_404(client, tmp_path):
    root = seed(tmp_path / "snap4")
    scan = client.post("/projects/scan", json={"root": str(root)}).json()["data"]
    pid = scan["project_id"]
    r = client.get(f"/projects/{pid}/scans/scan_missing")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
