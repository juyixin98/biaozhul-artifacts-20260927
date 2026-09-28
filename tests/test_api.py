"""端到端 API 测试：具体结果、失败类别、请求身份、版本管理。"""
from __future__ import annotations


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["active_version"] == "test-v1"
    assert "request_id" in body


def test_correct_basic(client):
    r = client.post("/v1/correct", json={"query": "speling", "threshold": 2.0})
    assert r.status_code == 200, r.text
    body = r.json()
    words = [c["word"] for c in body["candidates"]]
    assert words[0] == "spelling"
    assert body["candidates"][0]["distance"] == 1.0
    # 编辑路径可独立重算成本
    assert body["candidates"][0]["path_length"] >= 1
    assert body["request_id"]
    assert r.headers["x-request-id"] == body["request_id"]


def test_request_id_propagated(client):
    r = client.post("/v1/correct", json={"query": "hello"},
                    headers={"x-request-id": "fixed-id-1234"})
    assert r.headers["x-request-id"] == "fixed-id-1234"
    assert r.json()["request_id"] == "fixed-id-1234"


def test_casefold_query(client):
    r = client.post("/v1/correct", json={"query": "HELLO", "threshold": 1.0})
    assert r.status_code == 200
    assert r.json()["candidates"][0]["word"] == "hello"
    assert r.json()["normalization"]["steps"]


def test_empty_query_failure_category(client):
    r = client.post("/v1/correct", json={"query": "   "})
    assert r.status_code == 422
    body = r.json()
    assert body["failure_code"] == "empty_query"
    assert body["error"] is True
    assert body["request_id"]


def test_too_long_query_failure_category(client):
    r = client.post("/v1/correct", json={"query": "a" * 33})
    assert r.status_code == 422
    assert r.json()["failure_code"] == "query_too_long"


def test_negative_threshold_rejected_at_schema(client):
    r = client.post("/v1/correct", json={"query": "abc", "threshold": -1})
    assert r.status_code == 422


def test_cost_override_transpose_cheaper(client):
    payload = {
        "query": "hlelo",
        "threshold": 1.0,
        "costs": {"transpose": 0.3, "substitute": 2.0, "insert": 2.0, "delete": 2.0},
    }
    r = client.post("/v1/correct", json=payload)
    assert r.status_code == 200, r.text
    cand = r.json()["candidates"][0]
    assert cand["word"] == "hello"
    assert cand["distance"] == 0.3


def test_negative_cost_override_rejected(client):
    r = client.post("/v1/correct", json={
        "query": "abc", "costs": {"insert": -2.0},
    })
    assert r.status_code == 422


def test_version_create_and_activate(client):
    r = client.post("/versions", json={
        "entries": [{"word": "alpha", "freq": 1}, {"word": "beta", "freq": 2}],
        "version_id": "v2", "activate": False, "source": "test",
    })
    assert r.status_code == 200, r.text
    # v2 未激活，纠错仍走 test-v1
    assert client.get("/health").json()["active_version"] == "test-v1"

    r = client.post("/versions/v2/activate")
    assert r.status_code == 200
    assert client.get("/health").json()["active_version"] == "v2"


def test_activate_missing_version_404(client):
    r = client.post("/versions/does-not-exist/activate")
    assert r.status_code == 404
    assert r.json()["failure_code"] == "version_not_found"


def test_duplicate_version_id_conflict(client):
    payload = {"entries": [{"word": "z", "freq": 1}], "version_id": "test-v1"}
    r = client.post("/versions", json=payload)
    assert r.status_code == 409


def test_diagnostics_stages_present(client):
    r = client.post("/v1/correct", json={"query": "pythom", "threshold": 1.5})
    body = r.json()
    stages = [s["name"] for s in body["diagnostics"]["stages"]]
    assert stages == ["normalize", "validate", "generate", "prune", "cap",
                      "evaluate", "rank"]
    assert body["candidates"][0]["word"] == "python"
