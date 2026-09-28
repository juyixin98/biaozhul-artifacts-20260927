"""验证接口测试：HTTP 状态码映射、dry-run/commit、查询接口、四类错误可区分。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from merge_engine.api import create_app


@pytest.fixture
def client(tmp_path):
    # 测试服务器显式开启故障注入，便于经 HTTP 验证提交失败分类
    app = create_app(tmp_path / "api.db", tmp_path / "journal",
                     allow_fault_injection=True)
    return TestClient(app), tmp_path


def _seed(tmp_path, table, columns, rows):
    from merge_engine import store as s
    conn = s.connect(tmp_path / "api.db")
    try:
        s.ensure_meta(conn)
        s.seed_target(conn, table, columns, rows)
        conn.commit()
    finally:
        conn.close()


def test_healthz(client):
    c, _ = client
    assert c.get("/healthz").json() == {"status": "ok"}


def test_dry_run_then_commit_http_flow(client):
    c, root = client
    _seed(root, "api1", ["k1", "k2", "v"], [{"k1": "a", "k2": 1, "v": 1}])
    body = {
        "source": {"format": "records", "records": [
            {"k1": "a", "k2": 1, "v": 2},
            {"k1": "b", "k2": 2, "v": 3},
        ]},
        "config": {"target_table": "api1", "key_columns": ["k1", "k2"]},
    }
    r1 = c.post("/v1/merge/dry-run", json=body)
    assert r1.status_code == 200
    data1 = r1.json()
    assert data1["status"] == "PLANNED"
    types = [a["type"] for a in data1["plan"]["actions"]]
    assert types == ["UPDATE_MATCHED", "INSERT_UNMATCHED"]

    # dry-run 后目标未变
    rows = c.get("/v1/targets/api1/rows").json()["rows"]
    assert len(rows) == 1 and rows[0]["v"] == 1

    # 正式提交
    r2 = c.post("/v1/merge", json=body)
    assert r2.status_code == 200
    assert r2.json()["status"] == "COMMITTED"
    rows = c.get("/v1/targets/api1/rows").json()["rows"]
    assert {(r["k1"], r["k2"]): r["v"] for r in rows} == {("a", 1): 2, ("b", 2): 3}


def test_source_duplicate_returns_400(client):
    c, root = client
    _seed(root, "api2", ["k1", "k2"], [])
    r = c.post("/v1/merge", json={
        "source": {"format": "records", "records": [
            {"k1": "a", "k2": 1}, {"k1": "a", "k2": 1}]},
        "config": {"target_table": "api2", "key_columns": ["k1", "k2"]},
    })
    assert r.status_code == 400  # 输入错误（源内同键多行）
    err = r.json()["error"]
    assert err["category"] == "INPUT_ERROR"
    assert err["code"] == "SOURCE_DUPLICATE_KEY"


def test_target_duplicate_returns_409_category(client):
    c, root = client
    _seed(root, "api3", ["k1", "k2"], [
        {"k1": "a", "k2": 1}, {"k1": "a", "k2": 1}])
    r = c.post("/v1/merge", json={
        "source": {"format": "records", "records": [{"k1": "a", "k2": 1}]},
        "config": {"target_table": "api3", "key_columns": ["k1", "k2"]},
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TARGET_DUPLICATE_KEY"
    assert r.json()["error"]["category"] == "STATE_CONFLICT"


def test_malformed_request_is_422_validation(client):
    c, _ = client
    r = c.post("/v1/merge", json={"source": {"format": "weird"}})
    assert r.status_code == 422  # pydantic 请求体校验


def test_injected_disk_full_is_507_and_no_partial_update(client):
    c, root = client
    _seed(root, "api4", ["k1", "k2", "v"], [{"k1": "a", "k2": 1, "v": 1}])
    r = c.post("/v1/merge", json={
        "source": {"format": "records", "records": [{"k1": "a", "k2": 1, "v": 9}]},
        "config": {"target_table": "api4", "key_columns": ["k1", "k2"]},
        "fault_point": "before_commit",
    })
    assert r.status_code == 507
    err = r.json()["error"]
    assert err["category"] == "RESOURCE_EXHAUSTED"
    assert err["code"] == "DISK_FULL"
    rows = c.get("/v1/targets/api4/rows").json()["rows"]
    assert rows[0]["v"] == 1


def test_injected_commit_io_error_is_500_and_rolled_back(client):
    c, root = client
    _seed(root, "api4b", ["k1", "k2", "v"], [{"k1": "a", "k2": 1, "v": 1}])
    r = c.post("/v1/merge", json={
        "source": {"format": "records", "records": [{"k1": "a", "k2": 1, "v": 9}]},
        "config": {"target_table": "api4b", "key_columns": ["k1", "k2"]},
        "fault_point": "commit_raises",
    })
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["category"] == "COMPUTATION_FAILURE"
    assert err["code"] == "COMMIT_FAILED"
    rows = c.get("/v1/targets/api4b/rows").json()["rows"]
    assert rows[0]["v"] == 1


def test_injected_failure_after_actions_is_500_no_partial_update(client):
    c, root = client
    _seed(root, "api4c", ["k1", "k2", "v"], [{"k1": "a", "k2": 1, "v": 1}])
    r = c.post("/v1/merge", json={
        "source": {"format": "records", "records": [
            {"k1": "a", "k2": 1, "v": 9},
            {"k1": "n", "k2": 2, "v": 8}]},
        "config": {"target_table": "api4c", "key_columns": ["k1", "k2"]},
        "fault_point": "after_actions",
    })
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "COMMIT_FAILED"
    rows = c.get("/v1/targets/api4c/rows").json()["rows"]
    assert len(rows) == 1 and rows[0]["v"] == 1


def test_fault_injection_disabled_by_default(tmp_path):
    from fastapi.testclient import TestClient
    app = create_app(tmp_path / "d.db", tmp_path / "j", allow_fault_injection=False)
    c = TestClient(app)
    r = c.post("/v1/merge", json={
        "source": {"format": "records", "records": []},
        "config": {"target_table": "x", "key_columns": ["k1"]},
        "fault_point": "after_actions",
    })
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "FAULT_INJECTION_DISABLED"


def test_run_lookup_and_actions(client):
    c, root = client
    _seed(root, "api5", ["k1", "k2"], [{"k1": "a", "k2": 1}])
    r = c.post("/v1/merge", json={
        "source": {"format": "records", "records": [{"k1": "a", "k2": 1, "v": "z"}]},
        "config": {"target_table": "api5", "key_columns": ["k1", "k2"]},
    })
    run_id = r.json()["run_id"]
    got = c.get(f"/v1/runs/{run_id}")
    assert got.status_code == 200 and got.json()["status"] == "COMMITTED"
    acts = c.get(f"/v1/runs/{run_id}/actions").json()["actions"]
    assert acts[0]["type"] == "UPDATE_MATCHED"
    assert c.get("/v1/runs/nope").status_code == 404
