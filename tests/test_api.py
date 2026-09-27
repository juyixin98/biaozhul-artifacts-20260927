"""FastAPI 端到端集成测试。

使用临时 SQLite + TestClient，断言具体结果、具体失败类别（HTTP 状态码
与 error.category）、请求身份关联与诊断轨迹，而非“接口能调用”。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.storage.version_store import VersionStore


@pytest.fixture()
def client(tmp_path, fixture):
    store = VersionStore(str(tmp_path / "api.sqlite3"))
    from tests.conftest import seed_from_fixture

    store = seed_from_fixture(str(tmp_path / "api_seeded.sqlite3"), fixture)
    app = create_app(store=store)
    with TestClient(app) as c:
        yield c, store


def _post_query(client_obj, body, request_id=None):
    headers = {"X-Request-ID": request_id} if request_id else {}
    return client_obj.post("/query", json=body, headers=headers)


def test_health_lists_latest_version(client):
    c, _ = client
    resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.json()["latest_version"] == 3


def test_query_returns_concrete_result_and_stats(client, fixture):
    c, _ = client
    resp = _post_query(c, {"query": "cat AND dog", "version": 2}, request_id="rid-concrete")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["result"]["doc_ids"] == fixture["expected_v2"]["cat AND dog"] == [1, 25]
    assert body["stats"]["blocks_skipped"] >= 1
    assert resp.headers["X-Request-ID"] == "rid-concrete"
    assert body["request_id"] == "rid-concrete"
    # 轨迹内联返回，且记录了解析、全集加载、节点求值等关键步骤
    stages = {s["stage"] for s in body["trace"]["steps"]}
    assert {"parse", "load_universe", "eval_and", "eval_not"} - stages <= {"eval_not"}


def test_all_fixture_queries_over_http_match_expected(client, fixture):
    c, _ = client
    for version, expected_map in ((2, fixture["expected_v2"]), (3, fixture["expected_v3"])):
        for query, expected in expected_map.items():
            resp = _post_query(c, {"query": query, "version": version})
            assert resp.status_code == 200, query
            assert resp.json()["result"]["doc_ids"] == expected, (version, query)


def test_not_is_scoped_to_explicit_version_universe(client):
    c, _ = client
    # v3 删除了 9/25/40：NOT cat 不得包含它们
    resp = _post_query(c, {"query": "NOT cat", "version": 3})
    ids = resp.json()["result"]["doc_ids"]
    assert set(ids).isdisjoint({9, 25, 40})
    assert max(ids) <= 40 and min(ids) >= 1


def test_full_negation_over_http_is_empty(client):
    c, _ = client
    resp = _post_query(c, {"query": "NOT everything", "version": 2})
    assert resp.json()["result"]["doc_ids"] == []


def test_parse_error_category_and_status(client):
    c, _ = client
    resp = _post_query(c, {"query": "cat AND", "version": 2})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["category"] == "parse_error"
    assert "期待词项" in err["message"]


def test_unknown_term_category_and_status(client):
    c, _ = client
    resp = _post_query(c, {"query": "cat AND ghost", "version": 2})
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["category"] == "unknown_term"


def test_unknown_term_empty_flag_records_uncertainty(client):
    c, _ = client
    resp = _post_query(
        c,
        {"query": "cat AND ghost", "version": 2, "unknown_terms_empty": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["doc_ids"] == []
    assert body["uncertainties"][0]["reason"].startswith("未知词项")


def test_version_not_found_category(client):
    c, _ = client
    resp = _post_query(c, {"query": "cat", "version": 99})
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "version_not_found"


def test_operand_orders_agree_over_http(client, fixture):
    c, _ = client
    q = "cat AND dog AND fish"
    lr = _post_query(c, {"query": q, "version": 2, "operand_order": "left_to_right"}).json()
    rl = _post_query(c, {"query": q, "version": 2, "operand_order": "right_to_left"}).json()
    assert lr["result"]["doc_ids"] == rl["result"]["doc_ids"]
    assert lr["result"]["doc_ids"] == fixture["expected_v2"][q]


def test_commit_then_query_new_version(client):
    c, store = client
    resp = c.post(
        "/versions/commit",
        json={
            "adds": [{"doc_id": 41, "terms": ["cat", "newt"]}],
            "deletes": [1],
            "message": "集成测试新版本",
        },
    )
    assert resp.status_code == 200
    new_version = resp.json()["version_id"]
    assert new_version == 4

    # 新版本：doc 1 已删除，doc 41 已加入
    q = _post_query(c, {"query": "cat", "version": new_version}).json()
    assert q["result"]["doc_ids"] == [17, 33, 41]
    qn = _post_query(c, {"query": "newt", "version": new_version}).json()
    assert qn["result"]["doc_ids"] == [41]
    # 旧版本保持不可变
    old = _post_query(c, {"query": "cat", "version": 3}).json()
    assert old["result"]["doc_ids"] == [1, 17, 33]


def test_delete_nonexistent_is_conflict(client):
    c, _ = client
    resp = c.post("/versions/commit", json={"deletes": [9999]})
    assert resp.status_code == 409
    assert resp.json()["error"]["category"] == "version_conflict"


def test_trace_can_be_retrieved_by_request_id(client):
    c, _ = client
    _post_query(c, {"query": "cat AND dog", "version": 2}, request_id="rid-trace-123")
    resp = c.get("/diagnostics/traces/rid-trace-123")
    assert resp.status_code == 200
    trace = resp.json()["trace"]
    assert trace["request_id"] == "rid-trace-123"
    assert trace["summary"]["result_count"] == 2


def test_failed_request_trace_is_also_retained(client):
    c, _ = client
    _post_query(c, {"query": "cat AND ghost", "version": 2}, request_id="rid-fail")
    resp = c.get("/diagnostics/traces/rid-fail")
    trace = resp.json()["trace"]
    assert trace["failures"][0]["category"] == "unknown_term"


def test_result_ids_are_unique_end_to_end(client):
    c, _ = client
    resp = _post_query(c, {"query": "(cat OR dog OR fish) AND NOT rare", "version": 2})
    ids = resp.json()["result"]["doc_ids"]
    assert len(ids) == len(set(ids))
