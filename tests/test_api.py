"""FastAPI audit interface tests (in-process, no network sockets)."""

import json

import pytest
from fastapi.testclient import TestClient

from conftest import GHP_TOKEN, SLACK_TOKEN
from secretscan.api import create_app
from secretscan.config import load_settings


@pytest.fixture
def client(tmp_path, rule_pack, scope_pack):
    settings = load_settings(tmp_path / "ws.db",
                             allowed_roots=(tmp_path,))
    app = create_app(settings, rule_pack, scope_pack)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    (d / "a.py").write_text(f'TOKEN = "{GHP_TOKEN}"\n')
    (d / "b.py").write_text(f'SLACK_BOT_TOKEN = "{SLACK_TOKEN}"\n')
    return d


def test_healthz_reports_offline_and_versions(client):
    resp = client.get("/healthz", headers={"X-Request-Id": "rid-health",
                                          "X-Actor-Id": "alice"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["offline"] is True
    assert body["request_id"] == "rid-health"
    assert body["actor_id"] == "alice"
    assert body["versions"]["rule_pack"].startswith("rules:")
    assert body["versions"]["scope_pack"].startswith("scope:")


def test_scan_endpoint_returns_masked_report_and_echoes_identity(client, repo):
    resp = client.post("/scans", json={"root": str(repo)},
                       headers={"X-Request-Id": "rid-1", "X-Actor-Id": "bob"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["request_id"] == "rid-1"
    assert body["actor_id"] == "bob"
    report = body["report"]
    assert report["versions"]["rule_pack"]
    assert report["counts"]["findings_new"] == 2
    # No full secret anywhere in the HTTP body.
    text = resp.text
    assert GHP_TOKEN not in text
    assert SLACK_TOKEN not in text
    # Masks ARE present.
    assert "ghp_" in text and "xoxb" in text
    # Disclaimer present.
    assert "candidate" in report["disclaimer"]


def test_scan_rejects_relative_root(client, tmp_path):
    resp = client.post("/scans", json={"root": "relative/path"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "root_not_absolute"
    assert "request_id" in resp.json()["detail"]


def test_scan_rejects_root_outside_allowed_roots(client, tmp_path):
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    try:
        resp = client.post("/scans", json={"root": str(outside)},
                           headers={"X-Request-Id": "rid-deny"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "root_not_allowed"
        assert resp.json()["detail"]["request_id"] == "rid-deny"
    finally:
        outside.rmdir()


def test_scan_rejects_missing_root(client):
    resp = client.post("/scans", json={"root": "/nonexistent/path/xyz"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "root_not_found"


def test_get_scan_roundtrip_and_404(client, repo):
    post = client.post("/scans", json={"root": str(repo)})
    scan_id = post.json()["report"]["scan_id"]
    got = client.get(f"/scans/{scan_id}")
    assert got.status_code == 200
    assert got.json()["report"]["scan_id"] == scan_id
    assert got.json()["report"]["counts"]["findings_new"] == 2
    missing = client.get("/scans/999")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "scan_not_found"


def test_findings_endpoint_lifecycle_filter(client, repo):
    client.post("/scans", json={"root": str(repo)},
                headers={"X-Request-Id": "r1"})
    # Second identical scan -> findings are now open.
    client.post("/scans", json={"root": str(repo)},
                headers={"X-Request-Id": "r2"})
    scans = client.get("/scans").json()["scans"]
    assert [s["id"] for s in scans] == [2, 1]

    open_resp = client.get("/scans/2/findings", params={"lifecycle": "open"})
    assert open_resp.status_code == 200
    open_findings = open_resp.json()["findings"]["open"]
    assert len(open_findings) == 2
    # Other lifecycle buckets are excluded.
    assert set(open_resp.json()["findings"].keys()) == {"open"}

    bad = client.get("/scans/2/findings", params={"lifecycle": "nope"})
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "unknown_lifecycle"


def test_finding_detail_aggregates_occurrences(client, repo):
    client.post("/scans", json={"root": str(repo)})
    listing = client.get("/scans/1/findings").json()["findings"]["new"]
    fid = next(f["finding_id"] for f in listing
               if f["rule_id"] == "github-classic-pat")
    detail = client.get(f"/findings/{fid}").json()["finding"]
    assert detail["rule_id"] == "github-classic-pat"
    assert detail["state"] == "new"
    assert detail["occurrences"][0]["relpath"] == "a.py"
    assert detail["occurrences"][0]["line"] == 1
    # Raw value absent, mask present.
    assert GHP_TOKEN not in json.dumps(detail)
    assert "*" in detail["mask"]
    assert client.get("/findings/9999").status_code == 404


def test_audit_trail_correlates_by_request_id_and_actor(client, repo):
    client.post("/scans", json={"root": str(repo)},
                headers={"X-Request-Id": "trace-me", "X-Actor-Id": "carol"})
    events = client.get("/audit", params={"request": "trace-me"}).json()["events"]
    assert events, "expected audit events for the request"
    actions = {e["action"] for e in events}
    assert "scan.started" in actions
    assert "finding.new" in actions
    assert "scan.completed" in actions
    assert all(e["request_id"] == "trace-me" for e in events)
    assert all(e["actor_id"] == "carol" for e in events)
    # Every finding event carries the mask (not the raw value) and rule id.
    finding_events = [e for e in events if e["action"] == "finding.new"]
    for e in finding_events:
        assert e["details"]["mask"]
        assert GHP_TOKEN not in json.dumps(e)
        assert e["details"]["rule_id"]
        assert e["outcome"] == "ok"


def test_audit_filter_by_action(client, repo):
    client.post("/scans", json={"root": str(repo)},
                headers={"X-Request-Id": "r9"})
    events = client.get("/audit", params={"action": "scan.completed"}).json()
    assert events["events"]
    assert all(e["action"] == "scan.completed" for e in events["events"])


def test_denied_scan_is_audited(client, tmp_path):
    outside = tmp_path.parent / f"evil-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    try:
        client.post("/scans", json={"root": str(outside)},
                    headers={"X-Request-Id": "denied-1"})
        events = client.get("/audit",
                            params={"request": "denied-1"}).json()
        assert any(e["action"] == "api.denied" and e["outcome"] == "denied"
                   for e in events["events"])
    finally:
        outside.rmdir()
