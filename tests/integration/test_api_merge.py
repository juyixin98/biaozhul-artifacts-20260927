"""FastAPI + SQLite + Parquet 端到端集成测试。

通过 httpx ASGI 传输走真实 HTTP 语义（状态码、错误信封），不断言"能调用"，
而断言具体行集、具体状态码与具体 error.code。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from merge3.api.app import create_app

FIELDS = [
    {"name": "id", "type": "int64", "nullable": False},
    {"name": "status", "type": "string"},
    {"name": "amount", "type": "int64"},
    {"name": "owner", "type": "string"},
]


@pytest.fixture
def client(cfg):
    return TestClient(create_app(cfg))


def _bootstrap(client):
    r = client.post("/api/v1/tables", json={
        "name": "orders", "primary_key": ["id"], "fields": FIELDS})
    assert r.status_code == 201, r.text
    base = [
        {"id": 1, "status": "new", "amount": 100, "owner": "alice"},
        {"id": 2, "status": "new", "amount": 200, "owner": "bob"},
        {"id": 3, "status": "paid", "amount": 300, "owner": "carol"},
        {"id": 4, "status": "new", "amount": 400, "owner": "dave"},
    ]
    r = client.post("/api/v1/tables/orders/snapshots", json={"rows": base})
    sid = r.json()["snapshot"]["snapshot_id"]
    assert r.status_code == 201
    for b in ("main", "develop"):
        assert client.post(f"/api/v1/tables/orders/branches/{b}",
                           json={"head_snapshot_id": sid}).status_code == 201
    return sid, base


def test_health_and_404_envelope(client):
    assert client.get("/health").json()["status"] == "ok"
    r = client.get("/api/v1/tables/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_validation_error_shape_not_fake_success(client):
    r = client.post("/api/v1/tables", json={"name": "x", "primary_key": [], "fields": []})
    assert r.status_code == 422  # pydantic 形状校验
    r = client.post("/api/v1/tables", json={
        "name": "orders", "primary_key": ["id"], "fields": FIELDS})
    assert r.status_code == 201
    # 主键重复是领域校验错误，不能变成成功
    dup = [
        {"id": 1, "status": "a", "amount": 1, "owner": "x"},
        {"id": 1, "status": "b", "amount": 2, "owner": "y"},
    ]
    r = client.post("/api/v1/tables/orders/snapshots", json={"rows": dup})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_full_merge_lifecycle_over_http(client, cfg):
    base_id, base = _bootstrap(client)

    dev = [
        {"id": 1, "status": "paid", "amount": 100, "owner": "alice"},   # 冲突字段 status
        {"id": 2, "status": "new", "amount": 200, "owner": "bob-new"},   # dev 改 owner
        {"id": 3, "status": "paid", "amount": 300, "owner": "carol"},
        # id=4 dev 保留
        {"id": 5, "status": "new", "amount": 500, "owner": "erin"},      # dev 新增
    ]
    main = [
        {"id": 1, "status": "void", "amount": 100, "owner": "alice"},
        {"id": 2, "status": "new", "amount": 220, "owner": "bob"},       # main 改 amount
        {"id": 3, "status": "paid", "amount": 300, "owner": "carol"},
        # id=4 main 删除
        {"id": 6, "status": "new", "amount": 600, "owner": "frank"},     # main 新增
    ]
    client.post("/api/v1/tables/orders/branches/develop/commits", json={"rows": dev})
    client.post("/api/v1/tables/orders/branches/main/commits", json={"rows": main})

    r = client.post("/api/v1/merges", json={"table": "orders"})
    assert r.status_code == 201
    view = r.json()
    run_id = view["run_id"]
    assert view["counts"] == {"total": 6, "conflicts": 1, "unresolved": 1}
    conflict = view["conflicts"][0]
    assert conflict["classification"] == "field_value_conflict"
    assert conflict["key"] == {"id": 1}
    # 自动分区结果具体可见
    entries = view["entries"]
    assert entries["[2]"]["merged"]["amount"] == 220
    assert entries["[2]"]["merged"]["owner"] == "bob-new"
    assert entries["[4]"]["deleted"] is True
    assert entries["[5]"]["classification"] == "ours_added"
    assert entries["[6]"]["classification"] == "theirs_added"

    # 带冲突提交 -> 409 + 具体失败类别
    r = client.post(f"/api/v1/merges/{run_id}/commit", json={})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "unresolved_conflicts"

    # 错误绑定被拒
    r = client.post(f"/api/v1/merges/{run_id}/resolve", json={
        "key": [1], "kind": "ours",
        "bound_snapshots": [base_id, view["ours_snapshot_id"], "snap_deadbeef"]})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "resolution_binding_mismatch"

    # 解决方案越界改非冲突字段 -> 422 resolution_rejected
    r = client.post(f"/api/v1/merges/{run_id}/resolve", json={
        "key": [1], "kind": "value", "custom_row": {"status": "x", "amount": 1},
        "bound_snapshots": view["bound_snapshots"]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "resolution_rejected"

    # 正确解决并提交
    r = client.post(f"/api/v1/merges/{run_id}/resolve", json={
        "key": [1], "kind": "value", "custom_row": {"status": "paid*"},
        "bound_snapshots": view["bound_snapshots"]})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/v1/merges/{run_id}/commit",
                    json={"message": "merge develop into main"})
    assert r.status_code == 201, r.text
    merged_id = r.json()["merged_snapshot"]["snapshot_id"]

    rows = client.get(f"/api/v1/tables/orders/snapshots/{merged_id}/rows").json()["rows"]
    final = {r["id"]: r for r in rows}
    assert sorted(final) == [1, 2, 3, 5, 6]
    assert final[1]["status"] == "paid*"
    assert final[2] == {"id": 2, "status": "new", "amount": 220, "owner": "bob-new"}

    # 血缘：合并节点两条父边；旧头仍可达
    g = client.get("/api/v1/tables/orders/lineage").json()
    edges = {(e["child"], e["parent"]) for e in g["edges"]}
    assert (merged_id, view["ours_snapshot_id"]) in edges
    assert (merged_id, view["theirs_snapshot_id"]) in edges
    main_branch = next(b for b in g["branches"] if b["name"] == "main")
    assert main_branch["head_snapshot_id"] == merged_id

    # 合并后 Parquet 文件确实存在且不可变（重复读取一致）
    rows2 = client.get(f"/api/v1/tables/orders/snapshots/{merged_id}/rows").json()
    assert rows2["rows"] == rows


def test_delete_modify_conflict_can_be_resolved_both_ways(client):
    base_id, base = _bootstrap(client)
    dev = [r for r in base if r["id"] != 3]                      # dev 删除 id=3
    main = [dict(r, status="refunded") if r["id"] == 3 else r    # main 修改 id=3
            for r in base]
    client.post("/api/v1/tables/orders/branches/develop/commits", json={"rows": dev})
    client.post("/api/v1/tables/orders/branches/main/commits", json={"rows": main})

    run = client.post("/api/v1/merges", json={"table": "orders"}).json()
    assert run["counts"]["conflicts"] == 1
    assert run["conflicts"][0]["classification"] == "delete_modify_conflict"
    rid = run["run_id"]

    # 选择删除
    client.post(f"/api/v1/merges/{rid}/resolve", json={
        "key": [3], "kind": "delete", "bound_snapshots": run["bound_snapshots"]})
    out = client.post(f"/api/v1/merges/{rid}/commit", json={}).json()
    merged_id = out["merged_snapshot"]["snapshot_id"]
    rows = client.get(f"/api/v1/tables/orders/snapshots/{merged_id}/rows").json()["rows"]
    assert 3 not in {r["id"] for r in rows}

    # 删除解决已并入共同历史：再次合并时 id=3 在三方都不存在，无冲突且不复活
    run_again = client.post("/api/v1/merges", json={"table": "orders"}).json()
    assert run_again["counts"]["conflicts"] == 0
    assert "[3]" not in run_again["entries"]

    # 新一轮删除/修改冲突，这次选择 KEEP（保留 main 的修改版本）
    # develop 从当前 main 合并结果分叉：删除 id=2；main 修改 id=2
    current = client.get(
        f"/api/v1/tables/orders/snapshots/{merged_id}/rows").json()["rows"]
    dev_rows = [r for r in current if r["id"] != 2]
    main_rows = [dict(r, owner="bob-main") if r["id"] == 2 else r for r in current]
    client.post("/api/v1/tables/orders/branches/develop/commits", json={"rows": dev_rows})
    client.post("/api/v1/tables/orders/branches/main/commits", json={"rows": main_rows})
    run3 = client.post("/api/v1/merges", json={"table": "orders"}).json()
    assert run3["conflicts"][0]["classification"] == "delete_modify_conflict"
    assert run3["conflicts"][0]["key"] == {"id": 2}
    client.post(f"/api/v1/merges/{run3['run_id']}/resolve", json={
        "key": [2], "kind": "keep", "bound_snapshots": run3["bound_snapshots"]})
    out3 = client.post(f"/api/v1/merges/{run3['run_id']}/commit", json={}).json()
    rows3 = client.get(
        f"/api/v1/tables/orders/snapshots/{out3['merged_snapshot']['snapshot_id']}/rows"
    ).json()["rows"]
    assert next(r for r in rows3 if r["id"] == 2)["owner"] == "bob-main"


def test_unknown_exception_is_500_internal_not_success(cfg):
    # raise_server_exceptions=False：按真实 HTTP 服务端行为验证 500 信封，
    # 而不是让 TestClient 把异常抛回测试进程
    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    _bootstrap(client)
    # develop 加一行后合并：无冲突，commit 阶段被注入未知异常
    client.post("/api/v1/tables/orders/branches/develop/commits",
                json={"rows": [
                    {"id": 1, "status": "new", "amount": 100, "owner": "alice"},
                    {"id": 2, "status": "new", "amount": 200, "owner": "bob"},
                    {"id": 3, "status": "paid", "amount": 300, "owner": "carol"},
                    {"id": 4, "status": "new", "amount": 400, "owner": "dave"},
                    {"id": 9, "status": "new", "amount": 9, "owner": "late"},
                ]})
    run = client.post("/api/v1/merges", json={"table": "orders"}).json()
    assert run["counts"]["conflicts"] == 0

    def boom(*a, **k):
        raise RuntimeError("synthetic unexpected failure")

    client.app.state.service.commit_merge = boom
    r = client.post(f"/api/v1/merges/{run['run_id']}/commit", json={})
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "internal_error"
    assert "synthetic unexpected failure" in body["error"]["message"]
