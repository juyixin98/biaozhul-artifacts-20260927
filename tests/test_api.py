"""API 端到端测试：版本提交、对比、计划、作业状态与诊断脱敏。"""
import pytest
from fastapi.testclient import TestClient

from hlsplan.api import create_app
from hlsplan.config import Settings


@pytest.fixture
def client():
    app = create_app(Settings(db_path=":memory:"))
    with TestClient(app) as c:
        yield c


def _post_version(client, name, text):
    return client.post(
        f"/v1/playlists/{name}/versions",
        content=text.encode("utf-8"),
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
    )


def test_submit_compare_plan_flow(client, fixture_text):
    r1 = _post_version(client, "live", fixture_text("window_v1.m3u8"))
    assert r1.status_code == 201
    assert r1.json()["version"] == 1
    assert r1.json()["segment_count"] == 5
    assert r1.json()["request_id"]

    r2 = _post_version(client, "live", fixture_text("window_v2.m3u8"))
    assert r2.json()["version"] == 2

    cmp = client.post("/v1/compare",
                      json={"name": "live", "from_version": 1, "to_version": 2})
    assert cmp.status_code == 200
    diff = cmp.json()["diff"]
    assert diff["expired"] == [100, 101]
    assert diff["appended"] == [105, 106]
    assert diff["retracted"] == []
    job_id = cmp.json()["job_id"]

    job = client.get(f"/v1/jobs/{job_id}")
    assert job.status_code == 200
    assert job.json()["job"]["status"] == "DONE"
    assert job.json()["job"]["request_id"] == cmp.json()["request_id"]

    plan = client.post("/v1/plans", json={"name": "live", "version": 2})
    assert plan.status_code == 200
    body = plan.json()["plan"]
    assert [e["media_sequence"] for e in body["entries"]] == [102, 103, 104, 105, 106]
    assert body["runs"] == [
        {"start_sequence": 102, "end_sequence": 106, "discontinuity_sequence": 0,
         "start_time": 0.0, "end_time": 20.0, "segment_count": 5}]


def test_submit_duplicate_tag_422(client, fixture_text):
    r = _post_version(client, "bad", fixture_text("duplicate_tag.m3u8"))
    assert r.status_code == 422
    body = r.json()
    assert body["rejected"] is True
    assert "DUPLICATE_TAG" in body["failure_categories"]
    assert body["request_id"]


def test_submit_encrypted_422(client, fixture_text):
    r = _post_version(client, "enc", fixture_text("encrypted.m3u8"))
    assert r.status_code == 422
    assert "ENCRYPTION_UNSUPPORTED" in r.json()["failure_categories"]
    # 响应整体不得泄露密钥 URI 的 session 参数
    assert "SECRETKEY" not in r.text


def test_append_after_endlist_409(client, fixture_text):
    _post_version(client, "vod", fixture_text("endlist_v1.m3u8"))
    _post_version(client, "vod", fixture_text("endlist_v2_append.m3u8"))
    cmp = client.post("/v1/compare",
                      json={"name": "vod", "from_version": 1, "to_version": 2})
    assert cmp.status_code == 409
    body = cmp.json()
    assert body["diff"]["rejected"] is True
    codes = {d["code"] for d in body["diagnostics"]}
    assert "APPEND_AFTER_ENDLIST" in codes


def test_conflict_uris_redacted_in_response(client, fixture_text):
    _post_version(client, "c", fixture_text("window_v1.m3u8"))
    _post_version(client, "c", fixture_text("conflict_v2.m3u8"))
    cmp = client.post("/v1/compare",
                      json={"name": "c", "from_version": 1, "to_version": 2})
    assert cmp.status_code == 200
    assert "SECRET9" not in cmp.text
    kinds = {(x["media_sequence"], x["kind"]) for x in cmp.json()["diff"]["conflicts"]}
    assert (103, "URI_CONFLICT") in kinds
    assert (104, "DURATION_CONFLICT") in kinds


def test_compare_unknown_version_404(client, fixture_text):
    _post_version(client, "x", fixture_text("window_v1.m3u8"))
    r = client.post("/v1/compare", json={"name": "x", "from_version": 9})
    assert r.status_code == 404
    codes = {d["code"] for d in r.json()["diagnostics"]}
    assert "NOT_FOUND" in codes


def test_job_not_found_404(client):
    r = client.get("/v1/jobs/nonexistent")
    assert r.status_code == 404


def test_request_id_header(client, fixture_text):
    r = _post_version(client, "h", fixture_text("window_v1.m3u8"))
    assert r.headers["X-Request-Id"] == r.json()["request_id"]
