"""过滤与列裁剪不得改变删除语义；NULL 三值逻辑测试。"""
from __future__ import annotations

from conftest import delete_batch, load_inline, snapshot, verdict_index


def _setup(client, table="t"):
    client.post("/tables", json={
        "table_id": table,
        "columns": {"id": "int64", "name": "string", "age": "int64"},
        "key": ["id"],
    })
    load_inline(client, table, "f", [
        {"id": 1, "name": "a", "age": 10},
        {"id": 2, "name": "b", "age": 20},
        {"id": 3, "name": "c", "age": 30},
        {"id": None, "name": "nullid", "age": 5},
    ])


def test_projection_does_not_revive_deleted(client):
    _setup(client)
    # 裁剪掉主键列后删除 id=2，再只投影 name：被删行不得复活
    delete_batch(client, "t", [{"delete_id": "d", "kind": "equality", "key": {"id": 2}}])
    r = client.post("/tables/t/query", json={"columns": ["name"]})
    assert r.status_code == 200
    names = sorted(row["values"]["name"] for row in r.json()["result"]["rows"])
    assert names == ["a", "c", "nullid"]
    assert all("id" not in row["values"] for row in r.json()["result"]["rows"])
    assert r.json()["result"]["deleted_count"] == 1


def test_filter_only_applies_to_survivors(client):
    _setup(client)
    delete_batch(client, "t", [{"delete_id": "d", "kind": "equality", "key": {"id": 2}}])
    # age>=20 若错误地在删除前执行，会保留 id=2；正确语义是先删后滤
    r = client.post("/tables/t/query", json={"filters": [
        {"column": "age", "op": "gte", "value": 20}]})
    ids = [row["values"]["id"] for row in r.json()["result"]["rows"]]
    assert ids == [3]


def test_filter_eq_null_semantics(client):
    _setup(client)
    # is_null 能找出 NULL 行；eq null 不匹配任何行（三值逻辑）
    r1 = client.post("/tables/t/query", json={"filters": [
        {"column": "id", "op": "is_null"}]})
    assert [row["values"]["name"] for row in r1.json()["result"]["rows"]] == ["nullid"]
    r2 = client.post("/tables/t/query", json={"filters": [
        {"column": "id", "op": "eq", "value": None}]})
    assert r2.json()["result"]["rows"] == []
    # neq 也不匹配 NULL
    r3 = client.post("/tables/t/query", json={"filters": [
        {"column": "id", "op": "neq", "value": 1}]})
    assert sorted(row["values"]["id"] for row in r3.json()["result"]["rows"]) == [2, 3]


def test_equality_delete_null_predicate_deletes_nothing(client):
    _setup(client)
    r = delete_batch(client, "t", [{
        "delete_id": "dn", "kind": "equality", "key": {"id": None}}])
    assert r.status_code == 200
    item = r.json()["result"]["results"][0]
    assert item["matched_rows"] == []
    # 操作状态：applied_zero_rows；NULL 行的保留依据明确
    snap = snapshot(client, "t")
    idx = verdict_index(snap)
    null_row = idx[("f", 3)]
    assert null_row["action"] == "keep"
    assert null_row["reason"] == "null_row_key_blocked"
    op = next(e for e in snap["op_evaluations"] if e["delete_id"] == "dn")
    assert op["status"] == "applied_zero_rows"


def test_composite_key_null_and_match(client):
    client.post("/tables", json={
        "table_id": "ck",
        "columns": {"a": "int64", "b": "string", "v": "int64"},
        "key": ["a", "b"],
    })
    load_inline(client, "ck", "f", [
        {"a": 1, "b": "x", "v": 1},
        {"a": 1, "b": None, "v": 2},
        {"a": None, "b": "x", "v": 3},
        {"a": 1, "b": "x", "v": 4},   # 完全重复键
    ])
    # (1,x) 删除两行重复键
    delete_batch(client, "ck", [{"delete_id": "d", "kind": "equality",
                                 "key": {"a": 1, "b": "x"}}])
    snap = snapshot(client, "ck")
    kept = [(v["values"]["v"]) for v in snap["verdicts"] if v["action"] == "keep"]
    assert sorted(kept) == [2, 3]
    # 谓词含 NULL 不命中：即使行 (1, NULL) 的 b 也是 NULL
    r = delete_batch(client, "ck", [{"delete_id": "dn", "kind": "equality",
                                    "key": {"a": 1, "b": None}}])
    assert r.status_code == 200
    assert r.json()["result"]["results"][0]["matched_rows"] == []
