"""HTTP surface tests (in-process ASGI, no network listener required)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from archguard.api import create_app  # noqa: E402

from fixtures_archive import ZipSpec, build_zip  # noqa: E402


class AsgiSession:
    """Minimal synchronous wrapper over httpx's async-only ASGITransport."""

    def __init__(self, app) -> None:
        self._transport = httpx.ASGITransport(app=app)
        self._client = httpx.AsyncClient(
            transport=self._transport, base_url="http://test"
        )

    def __enter__(self) -> "AsgiSession":
        return self

    def __exit__(self, *exc) -> None:
        asyncio.run(self._client.aclose())

    def get(self, url: str):
        return asyncio.run(self._client.get(url))

    def post(self, url: str, **kw):
        return asyncio.run(self._client.post(url, **kw))


@pytest.fixture
def client(svc):
    yield AsgiSession(create_app(svc["config"]))


def _upload(client, data, name="a.zip"):
    return client.post(
        "/api/v1/inspect",
        files={"file": (name, data, "application/octet-stream")},
    )


def test_healthz_reports_version_and_budgets(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "archguard"
    assert "max_total_bytes" in body["budgets"]


def test_inspect_accepted_200(client):
    data = build_zip([ZipSpec("a.txt", data=b"hello")])
    r = _upload(client, data)
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["status"] == "accepted"
    assert body["files"][0]["declared_path"] == "a.txt"


def test_inspect_rejected_422_with_category(client):
    data = build_zip([ZipSpec("../escape", data=b"x")])
    r = _upload(client, data, name="evil.zip")
    assert r.status_code == 422
    body = r.json()
    assert body["accepted"] is False
    assert body["failure"]["category"] == "PATH_TRAVERSAL"
    assert body["failure"]["detail"]
    # Failures still carry a correlation id and never look like success.
    assert body["run_id"] and body["status"] == "rejected"


def test_unknown_format_422(client):
    r = _upload(client, b"not an archive", name="x.dat")
    assert r.status_code == 422
    assert r.json()["failure"]["category"] == "FORMAT_UNSUPPORTED"


def test_runs_listing_and_events(client):
    data = build_zip([ZipSpec("a.txt", data=b"x")])
    body = _upload(client, data).json()
    run_id = body["run_id"]

    listing = client.get("/api/v1/runs").json()
    assert any(r["run_id"] == run_id for r in listing["runs"])

    one = client.get(f"/api/v1/runs/{run_id}")
    assert one.status_code == 200
    assert one.json()["run_id"] == run_id

    events = client.get(f"/api/v1/runs/{run_id}/events")
    assert events.status_code == 200
    names = [e["name"] for e in events.json()["events"]]
    assert "run_start" in names and ("accepted" in names)


def test_get_unknown_run_404(client):
    assert client.get("/api/v1/runs/doesnotexist").status_code == 404


def test_audit_verify_endpoint(client):
    _upload(client, build_zip([ZipSpec("a.txt", data=b"x")]))
    r = client.get("/api/v1/audit/verify")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_manifest_endpoint_signed(client):
    body = _upload(client, build_zip([ZipSpec("a.txt", data=b"x")])).json()
    r = client.get(f"/api/v1/manifests/{body['run_id']}")
    assert r.status_code == 200
    envelope = r.json()
    assert envelope["alg"] == "HMAC-SHA256"
    assert len(envelope["signature"]) == 64
