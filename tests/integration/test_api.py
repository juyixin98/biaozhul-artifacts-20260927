"""HTTP 端到端集成测试：FastAPI + SQLite + 独立 oracle。

覆盖：
- 稀疏/稠密列表、AND/OR/NOT 的具体结果（对照集合代数 oracle）
- 空全集（版本 0）与全否定
- 删除后查询：删除同步可见性、结果 ID 唯一
- explain：三种执行顺序结果一致且分别报告跳过块数
- 请求身份关联：响应头 X-Request-ID、客户端指定 ID、日志与 trace 可复现
- 失败类别具体（spec_error / version_not_found / trace_not_found）
"""
from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from tests._fixtures import term_sets_for_version

SAMPLE = json.loads(
    (pathlib.Path(__file__).resolve().parents[2] / "sample_data" / "sample.json")
    .read_text(encoding="utf-8")
)


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "integ.db"), log_dir=str(tmp_path / "logs")
    )
    with TestClient(app) as c:
        yield c, app


@pytest.fixture()
def seeded_client(client):
    c, app = client
    for batch in SAMPLE["versions"]:
        adds = {int(k): v for k, v in batch.get("adds", {}).items()}
        r = c.post(
            "/versions/commit",
            json={"adds": adds, "deletes": batch.get("deletes", [])},
        )
        assert r.status_code == 201, r.text
    return c, app


def _oracle_expected(app, expr: str, version: int) -> set[int]:
    from tests._oracle import oracle_answer

    term_sets, universe = term_sets_for_version(app.state.store, version)
    return oracle_answer(expr, term_sets, universe)


# ---------------------------------------------------------------------------
# 健康/元信息
# ---------------------------------------------------------------------------


def test_health_and_spec(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["latest_version"] == 0
    assert body["block_size"] == 8
    spec = c.get("/spec").json()
    assert "error_categories" in spec and spec["error_categories"]


def test_commit_then_query_flow(seeded_client):
    c, app = seeded_client
    assert c.get("/versions").json()["latest"] == 2

    # 稠密 ∩：alpha ∩ beta → v2 上文档 {1,2}
    r = c.get("/query", params={"expr": "alpha AND beta", "version": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == [1, 2]
    assert body["count"] == 2
    assert body["result"] == sorted(set(body["result"]))  # 唯一有序
    assert body["stats"]["comparisons"] >= 1
    # oracle 对照
    assert set(body["result"]) == _oracle_expected(app, "alpha AND beta", 2)


def test_sparse_dense_or_and_not_concrete_results(seeded_client):
    c, app = seeded_client
    cases = {
        "rareword": [3],  # 稀疏
        "common": [6, 7],  # 稠密（8 已删除）
        "alpha OR common": [1, 2, 3, 6, 7, 9],
        "alpha AND NOT common": [1, 2, 3, 9],
        "NOT alpha": [4, 5, 6, 7],  # 全否定；8 已删
        "(alpha OR epsilon) AND NOT beta": [3, 9],
    }
    for expr, want in cases.items():
        body = c.get(
            "/query", params={"expr": expr, "version": 2}
        ).json()
        assert body["ok"] is True, body
        assert body["result"] == want, f"{expr}: {body['result']} != {want}"
        assert set(body["result"]) == _oracle_expected(app, expr, 2)


def test_version0_empty_universe_all_negations_empty(seeded_client):
    c, app = seeded_client
    for expr in ["*", "NOT alpha", "NOT common", "a OR NOT b"]:
        body = c.get("/query", params={"expr": expr, "version": 0}).json()
        assert body["ok"]
        assert body["result"] == [], expr
        assert set(body["result"]) == _oracle_expected(app, expr, 0)


def test_default_version_is_latest(seeded_client):
    c, _ = seeded_client
    body = c.get("/query", params={"expr": "common"}).json()
    assert body["version"] == 2
    assert 8 not in body["result"]


def test_deleted_doc_absent_from_all_query_shapes(seeded_client):
    c, app = seeded_client
    for expr in ["全集", "common", "NOT alpha", "*"]:
        body = c.get("/query", params={"expr": expr, "version": 2}).json()
        assert 8 not in body["result"], expr
        assert body["result"] == sorted(set(body["result"]))
        assert set(body["result"]) == _oracle_expected(app, expr, 2)
    # 但删除前的版本里 8 仍然可见
    body = c.get("/query", params={"expr": "common", "version": 1}).json()
    assert body["result"] == [6, 7, 8]


def test_explain_reports_consistent_orders_and_skip_counts(seeded_client):
    c, app = seeded_client
    r = c.get(
        "/explain", params={"expr": "alpha AND beta AND gamma AND delta", "version": 2}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ex = body["explain"]
    assert ex["consistent_across_orders"] is True
    orders = ex["orders"]
    results = {k: orders[k]["result"] for k in orders}
    assert results["rare_first"] == results["textual"] == results["reverse"]
    assert set(results["rare_first"]) == _oracle_expected(
        app, "alpha AND beta AND gamma AND delta", 2
    )
    # 每种顺序都独立报告跳过块数（非负整数）
    for order_name, s in ex["block_skip_summary"].items():
        assert isinstance(s, int) and s >= 0, order_name
    # 明细步骤带 op 与 stats，可解释“在哪跳过”
    steps = orders["rare_first"]["steps"]
    assert any(st["op"] == "intersect" for st in steps)
    assert all("stats" in st and "label" in st for st in steps)


def test_short_circuit_flag_and_skipped_nodes(seeded_client):
    c, _ = seeded_client
    body = c.get(
        "/query",
        params={"expr": "ghost AND alpha AND beta", "version": 2},
    ).json()
    assert body["result"] == []
    assert body["short_circuited"] is True
    assert body["skipped_nodes"], "必须报告被短路跳过的节点"


# ---------------------------------------------------------------------------
# 失败类别
# ---------------------------------------------------------------------------


def test_spec_error_envelope_has_category_and_position(seeded_client):
    c, _ = seeded_client
    r = c.get("/query", params={"expr": "alpha AND)", "version": 2})
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["category"] == "spec_error"
    assert body["error"]["position"] == 9
    assert body["request_id"]


def test_version_not_found_is_404_with_category(seeded_client):
    c, _ = seeded_client
    r = c.get("/query", params={"expr": "alpha", "version": 77})
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "version_not_found"


def test_unknown_term_warning_uncertainty_separated(seeded_client):
    c, _ = seeded_client
    body = c.get(
        "/query", params={"expr": "alpha AND zzz_unknown", "version": 2}
    ).json()
    assert body["ok"] is True
    assert body["result"] == []
    assert body["warnings"] and body["uncertainty"]
    assert "zzz_unknown" in body["uncertainty"][0]


# ---------------------------------------------------------------------------
# 请求身份关联与失败可复现
# ---------------------------------------------------------------------------


def test_request_id_echoed_and_persisted(seeded_client):
    c, _ = seeded_client
    rid = "fixed-id-001"
    r = c.get(
        "/query",
        params={"expr": "alpha AND beta", "version": 2},
        headers={"X-Request-ID": rid},
    )
    assert r.headers["X-Request-ID"] == rid
    # 诊断回查
    tr = c.get(f"/diagnostics/requests/{rid}").json()
    assert tr["ok"]
    assert tr["trace"]["request_id"] == rid
    assert tr["trace"]["expression"] == "alpha AND beta"
    assert tr["trace"]["version"] == 2
    assert tr["trace"]["status"] == "ok"
    assert tr["trace"]["result_count"] == 2
    assert tr["trace"]["stats"]["blocks_skipped"] >= 0
    # 列表接口可见
    listing = c.get("/diagnostics/requests").json()
    assert any(t["request_id"] == rid for t in listing["traces"])


def test_failed_request_is_traceable_with_category(seeded_client):
    c, _ = seeded_client
    rid = "fixed-id-fail"
    r = c.get(
        "/query",
        params={"expr": "alpha AND", "version": 2},
        headers={"X-Request-ID": rid},
    )
    assert r.status_code == 400
    tr = c.get(f"/diagnostics/requests/{rid}").json()["trace"]
    assert tr["status"] == "error"
    assert tr["error_category"] == "spec_error"
    assert tr["error_message"]  # 失败原因非空、可解释


def test_trace_not_found_category(seeded_client):
    c, _ = seeded_client
    r = c.get("/diagnostics/requests/nope")
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "trace_not_found"


def test_jsonl_log_correlates_request_id(seeded_client, tmp_path):
    c, _ = seeded_client
    rid = "log-rid-9"
    r = c.get(
        "/query",
        params={"expr": "common", "version": 2},
        headers={"X-Request-ID": rid},
    )
    assert r.status_code == 200
    log_path = tmp_path / "logs" / "service.jsonl"
    lines = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines()]
    ids_events = [ln for ln in lines if ln.get("request_id") == rid]
    events = {ln["event"] for ln in ids_events}
    assert {"request_start", "request_done"} <= events
    done = next(ln for ln in ids_events if ln["event"] == "request_done")
    assert done["status"] == "ok"
    assert done["version"] == 2


# ---------------------------------------------------------------------------
# 版本管理端到端
# ---------------------------------------------------------------------------


def test_commit_delete_endpoint_then_query(client):
    c, app = client
    r = c.post(
        "/versions/commit",
        json={"adds": {"1": "a b", "2": "a c", "3": "b c"}},
    )
    assert r.status_code == 201
    assert r.json()["version"] == 1
    r = c.post("/versions/commit", json={"deletes": [2]})
    assert r.json()["version"] == 2
    body = c.get("/query", params={"expr": "a"}).json()
    assert body["result"] == [1]
    assert body["version"] == 2
    # term a 的持久 posting 仍包含 2（删除只同步全集）
    assert list(app.state.store.posting("a").ids) == [1, 2]


def test_empty_commit_returns_storage_error(client):
    c, _ = client
    r = c.post("/versions/commit", json={})
    assert r.status_code == 400
    assert r.json()["error"]["category"] == "storage_error"
