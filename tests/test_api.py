"""API 端到端测试: ingest → compare → plan → job 状态,含失败类别。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hlsdiff.api import create_app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def client():
    return TestClient(create_app(":memory:"))


def ingest(client, stream: str, fixture: str):
    body = {"content": (FIXTURES / fixture).read_text()}
    return client.post(f"/streams/{stream}/playlists", json=body)


def test_ingest_accept_and_reject(client):
    ok = ingest(client, "s1", "window_v1.m3u8")
    assert ok.status_code == 201
    payload = ok.json()
    assert payload["accepted"] is True
    assert payload["version_no"] == 1
    assert payload["state"]["media_sequence_range"] == [0, 4]
    assert payload["request_id"]

    bad = ingest(client, "s1", "duplicate_tag.m3u8")
    assert bad.status_code == 422
    assert bad.json()["accepted"] is False
    assert bad.json()["category"] == "duplicate-tag"


def test_compare_window_advance_via_api(client):
    ingest(client, "live", "window_v1.m3u8")
    ingest(client, "live", "window_v2.m3u8")
    resp = client.post("/streams/live/compare")
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "accept"
    assert data["report"]["window_advanced"] == [0, 1]
    assert data["report"]["appended"] == [5, 6]
    assert data["from_version"] == 1 and data["to_version"] == 2

    job = client.get(f"/jobs/{data['job_id']}").json()["job"]
    assert job["state"] == "DONE"
    assert job["kind"] == "compare"
    assert job["request_id"] == data["request_id"]


def test_compare_missing_segment_rejected_via_api(client):
    ingest(client, "live2", "window_v1.m3u8")
    ingest(client, "live2", "missing_segment_v2.m3u8")
    data = client.post("/streams/live2/compare").json()
    assert data["decision"] == "reject"
    assert data["report"]["retracted"] == [4]


def test_compare_append_after_endlist_via_api(client):
    ingest(client, "vod", "ended_v1.m3u8")
    ingest(client, "vod", "ended_v2_appended.m3u8")
    data = client.post("/streams/vod/compare").json()
    assert data["decision"] == "reject"
    assert any("append-after-endlist" in r for r in data["report"]["reasons"])


def test_compare_single_version_unprocessable(client):
    ingest(client, "solo", "window_v1.m3u8")
    resp = client.post("/streams/solo/compare")
    assert resp.status_code == 422
    assert resp.json()["category"] == "version-not-found"


def test_compare_unknown_stream_404(client):
    resp = client.post("/streams/ghost/compare")
    assert resp.status_code == 404
    assert resp.json()["category"] == "stream-not-found"


def test_plan_via_api(client):
    ingest(client, "br", "byterange_v1.m3u8")
    data = client.get("/streams/br/plan").json()
    assert data["total_duration"] == 24.0
    offsets = [(e["byte_range"]["offset"], e["byte_range"]["length"]) for e in data["entries"]]
    assert offsets == [(0, 100), (100, 150), (400, 120)]

    ingest(client, "disc", "discontinuity_v1.m3u8")
    data = client.get("/streams/disc/plan").json()
    assert len(data["boundaries"]) == 1
    assert data["boundaries"][0]["sequence"] == 2
    assert data["boundaries"][0]["timeline_offset"] == 8.0


def test_plan_unknown_stream_404(client):
    resp = client.get("/streams/ghost/plan")
    assert resp.status_code == 404


def test_job_not_found(client):
    resp = client.get("/jobs/nonexistent")
    assert resp.status_code == 404
    assert resp.json()["category"] == "job-not-found"


def test_no_external_media_access(client):
    """计划只引用播放列表中的 URI,服务本身不发起任何外部请求。

    这里通过夹具中的伪 URI 验证:计划原样返回 URI,不做可达性探测。
    """
    ingest(client, "local", "window_v1.m3u8")
    data = client.get("/streams/local/plan").json()
    assert [e["uri"] for e in data["entries"]] == [f"seg{i}.ts" for i in range(5)]
