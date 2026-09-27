"""FastAPI 端到端断言：成功响应的具体结果，以及按类别区分的失败响应。

失败用例断言 HTTP 状态码、error.category 与 position，
绝不接受“接口能调通/统一 200”。
"""

import pytest
from fastapi.testclient import TestClient

from service.main import create_app


@pytest.fixture()
def client(engine):
    # 直接复用测试引擎的路径（独立 tmp DB），但服务自己建一套组件
    app = create_app(
        settings=engine.settings,
        fixtures_path=None,
    )
    # fixtures_path=None 会落到仓库 fixtures；engine 的库是同一份 schema，重建即可
    return TestClient(app), engine


def test_health(client) -> None:
    c, _ = client
    resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_query_success_concrete_matches(client) -> None:
    c, _ = client
    resp = c.post("/query", json={"query": "apple AND (pie OR salad)"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["matches"] == ["d1", "d2", "d8", "d9"]
    assert body["count"] == 4
    assert set(body["budget_usage"]) == {"depth", "clauses"}
    assert body["idempotent"] is True
    assert len(body["run_id"]) == 12
    assert body["canonical"]["type"] in ("and",)


def test_empty_query_matches_all(client) -> None:
    c, _ = client
    resp = c.post("/query", json={"query": ""})
    assert resp.status_code == 200
    assert resp.json()["count"] == 10
    assert resp.json()["canonical"] == {"type": "empty"}


def test_version_storage_roundtrip(client) -> None:
    c, _ = client
    body = c.post("/query", json={"query": "apple AND pie"}).json()
    version = body["version"]
    resp = c.get(f"/queries/{version}")
    assert resp.status_code == 200
    assert resp.json()["version_hash"] == version
    assert c.get("/queries/nonexistent0000").status_code == 404


def test_equivalent_spellings_same_version(client) -> None:
    c, _ = client
    v1 = c.post("/query", json={"query": "apple AND pie"}).json()["version"]
    v2 = c.post("/query", json={"query": "pie AND apple"}).json()["version"]
    assert v1 == v2


def test_parse_error_400_with_position(client) -> None:
    c, _ = client
    resp = c.post("/query", json={"query": "a AND OR b"})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["category"] == "PARSE_ERROR"
    assert err["position"] == 7
    assert "OR" in err["message"]


def test_lexer_error_400_unterminated_quote(client) -> None:
    c, _ = client
    resp = c.post("/query", json={"query": '"oops'})
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == "LEXER_ERROR"
    assert resp.json()["error"]["position"] == 1


def test_unknown_field_422(client) -> None:
    c, _ = client
    resp = c.post("/query", json={"query": "foo:bar"})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["category"] == "FIELD_UNKNOWN"
    assert err["position"] == 1


def test_field_type_422(client) -> None:
    c, _ = client
    resp = c.post("/query", json={"query": "year:abc"})
    assert resp.status_code == 422
    assert resp.json()["error"]["category"] == "FIELD_TYPE"


def test_budget_413(client) -> None:
    c, _ = client
    text = "apple"
    for _ in range(8):
        text = f"{text} AND (apple"  # 每层只加开括号，原始树深 9
    text += ")" * 8
    resp = c.post("/query", json={"query": text})
    assert resp.status_code == 413
    assert resp.json()["error"]["category"] == "BUDGET_EXCEEDED"


def test_error_is_not_wrapped_as_success(client) -> None:
    c, _ = client
    for bad in ["a AND OR b", "foo:bar", 'year:"x y"', '"unclosed']:
        resp = c.post("/query", json={"query": bad})
        assert resp.status_code >= 400
        assert "error" in resp.json()
        assert "matches" not in resp.json()


def test_documents_listing(client) -> None:
    c, _ = client
    resp = c.get("/documents")
    assert resp.status_code == 200
    docs = resp.json()["documents"]
    assert len(docs) == 10
    assert {d["doc_id"] for d in docs} == {f"d{i}" for i in range(1, 11)}
    assert all(d["version"] == 1 for d in docs)
