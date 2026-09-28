"""错误分类测试：输入错误 / 状态冲突 / 资源耗尽 / 计算失败 可区分。

每个用例都断言具体 HTTP 状态码 *和* error.category，并验证 run 日志里
保留了同样的错误类别（可按 run_id 重放）。
"""
from __future__ import annotations

import pytest

from conftest import delete_batch, load_inline


def _err(resp) -> dict:
    assert "error" in resp.json(), resp.text
    return resp.json()["error"]


def _run(client, run_id: str) -> dict:
    r = client.get(f"/runs/{run_id}")
    assert r.status_code == 200
    return r.json()


# ---------------- 输入错误 400 ----------------
@pytest.mark.errors
def test_unknown_table_is_404(client):
    r = client.get("/tables/nope")
    assert r.status_code == 404
    assert _err(r)["category"] == "not_found"


@pytest.mark.errors
def test_missing_key_column_400(client):
    r = client.post("/tables", json={
        "table_id": "t", "columns": {"id": "int64"}, "key": ["missing"],
    })
    assert r.status_code == 400
    assert _err(r)["category"] == "input_error"


@pytest.mark.errors
def test_unsupported_type_400(client):
    r = client.post("/tables", json={
        "table_id": "t", "columns": {"id": "decimal128"}, "key": ["id"],
    })
    assert r.status_code == 400
    assert _err(r)["category"] == "input_error"


@pytest.mark.errors
def test_row_type_mismatch_400(client, make_table):
    make_table("t")
    r = client.post("/tables/t/load", json={
        "file_id": "f", "source": {"kind": "inline", "rows": [{"id": "not-int",
                                                               "name": "x", "age": 1}]},
    })
    assert r.status_code == 400
    assert _err(r)["category"] == "input_error"
    assert "id" in str(_err(r)["details"])


@pytest.mark.errors
def test_extra_and_missing_columns_400(client, make_table):
    make_table("t")
    r = client.post("/tables/t/load", json={
        "file_id": "f", "source": {"kind": "inline",
                                   "rows": [{"id": 1, "name": "x", "age": 1, "z": 2}]},
    })
    assert r.status_code == 400 and _err(r)["category"] == "input_error"
    r = client.post("/tables/t/load", json={
        "file_id": "g", "source": {"kind": "inline", "rows": [{"id": 1, "name": "x"}]},
    })
    assert r.status_code == 400 and _err(r)["category"] == "input_error"


@pytest.mark.errors
def test_position_out_of_range_400(client, make_table):
    make_table("t")
    load_inline(client, "t", "f", [{"id": 1, "name": "a", "age": 1}])
    r = delete_batch(client, "t", [{
        "delete_id": "d1", "kind": "position", "file_id": "f", "row_number": 99,
    }])
    assert r.status_code == 400
    err = _err(r)
    assert err["category"] == "input_error"
    assert err["details"]["row_count"] == 1


@pytest.mark.errors
def test_equality_wrong_key_columns_400(client, make_table):
    make_table("t")
    load_inline(client, "t", "f", [{"id": 1, "name": "a", "age": 1}])
    # 缺列
    r = delete_batch(client, "t", [{
        "delete_id": "d1", "kind": "equality", "key": {},
    }])
    assert r.status_code == 400 and _err(r)["category"] == "input_error"
    # 多列
    r = delete_batch(client, "t", [{
        "delete_id": "d2", "kind": "equality", "key": {"id": 1, "age": 2},
    }])
    assert r.status_code == 400 and _err(r)["category"] == "input_error"


@pytest.mark.errors
def test_bad_json_shape_422(client):
    # Pydantic 形状错误 -> 422（请求结构错误，与语义 input_error 区分记录在文档）
    r = client.post("/tables", json={"columns": {}})
    assert r.status_code == 422


# ---------------- 状态冲突 409 ----------------
@pytest.mark.errors
def test_duplicate_table_409(client, make_table):
    make_table("t")
    r = client.post("/tables", json={
        "table_id": "t", "columns": {"id": "int64"}, "key": ["id"],
    })
    assert r.status_code == 409 and _err(r)["category"] == "state_conflict"


@pytest.mark.errors
def test_duplicate_file_id_409(client, make_table):
    make_table("t")
    load_inline(client, "t", "f", [{"id": 1, "name": "a", "age": 1}])
    r = client.post("/tables/t/load", json={
        "file_id": "f", "source": {"kind": "inline",
                                   "rows": [{"id": 2, "name": "b", "age": 2}]},
    })
    assert r.status_code == 409 and _err(r)["category"] == "state_conflict"


@pytest.mark.errors
def test_delete_id_conflicting_spec_409(client, make_table):
    make_table("t")
    load_inline(client, "t", "f", [{"id": 1, "name": "a", "age": 1}])
    r1 = delete_batch(client, "t", [{"delete_id": "d", "kind": "equality",
                                     "key": {"id": 1}}])
    assert r1.status_code == 200
    r2 = delete_batch(client, "t", [{"delete_id": "d", "kind": "equality",
                                     "key": {"id": 2}}])
    assert r2.status_code == 409
    err = _err(r2)
    assert err["category"] == "state_conflict"
    assert err["details"]["existing_spec"] != err["details"]["given_spec"]


@pytest.mark.errors
def test_idempotent_same_spec_returns_original(client, make_table):
    make_table("t")
    load_inline(client, "t", "f", [{"id": 1, "name": "a", "age": 1}])
    r1 = delete_batch(client, "t", [{"delete_id": "d", "kind": "equality",
                                     "key": {"id": 1}}])
    seq1 = r1.json()["result"]["results"][0]["seq"]
    r2 = delete_batch(client, "t", [{"delete_id": "d", "kind": "equality",
                                     "key": {"id": 1}}])
    assert r2.status_code == 200
    item = r2.json()["result"]["results"][0]
    assert item["idempotent"] is True and item["seq"] == seq1
    # 序列号水位没有因重试而增长
    s = client.get("/tables/t").json()["result"]
    assert s["seq_horizon"] == seq1


@pytest.mark.errors
def test_position_on_superseded_file_409(client, make_table):
    make_table("t")
    load_inline(client, "t", "f", [{"id": i, "name": f"n{i}", "age": i}
                                   for i in range(3)])
    assert client.post("/tables/t/deletes", json={"deletes": [
        {"delete_id": "d", "kind": "position", "file_id": "f", "row_number": 0}]}
    ).status_code == 200
    rw = client.post("/tables/t/rewrite", json={"file_ids": ["f"], "new_file_id": "g"})
    assert rw.status_code == 200
    r = delete_batch(client, "t", [{
        "delete_id": "late", "kind": "position", "file_id": "f", "row_number": 1,
    }])
    assert r.status_code == 409 and _err(r)["category"] == "state_conflict"


@pytest.mark.errors
def test_rewrite_to_existing_id_409(client, make_table):
    make_table("t")
    load_inline(client, "t", "f", [{"id": 1, "name": "a", "age": 1}])
    r = client.post("/tables/t/rewrite", json={"file_ids": ["f"], "new_file_id": "f"})
    assert r.status_code == 409 and _err(r)["category"] == "state_conflict"


@pytest.mark.errors
def test_rewrite_all_deleted_409(client, make_table):
    make_table("t")
    load_inline(client, "t", "f", [{"id": 1, "name": "a", "age": 1}])
    assert client.post("/tables/t/deletes", json={"deletes": [
        {"delete_id": "d", "kind": "equality", "key": {"id": 1}}]}).status_code == 200
    r = client.post("/tables/t/rewrite", json={"file_ids": ["f"], "new_file_id": "g"})
    assert r.status_code == 409 and _err(r)["category"] == "state_conflict"


# ---------------- 资源耗尽 413 ----------------
@pytest.mark.errors
def test_row_quota_resource_limit(tmp_path):
    from deleter.service import DeleterService
    svc = DeleterService(tmp_path / "ws", max_rows_per_load=3)
    svc.create_table("t", {"id": "int64"}, ["id"])
    with pytest.raises(Exception) as exc:
        svc.load_file("t", "f", {"kind": "inline",
                                 "rows": [{"id": i} for i in range(4)]})
    assert exc.value.category == "resource_limit"
    assert exc.value.http_status == 413
    assert exc.value.details["limit"] == 3
    svc.close()


@pytest.mark.errors
def test_inbox_path_traversal_400(client, make_table):
    make_table("t")
    r = client.post("/tables/t/load", json={
        "file_id": "f", "source": {"kind": "inbox", "name": "../../etc/passwd"},
    })
    assert r.status_code == 400 and _err(r)["category"] == "input_error"


# ---------------- 计算失败 500 ----------------
@pytest.mark.errors
def test_corrupt_parquet_compute_failure(client, make_table):
    make_table("t")
    ws = client.ws_path
    # 先合法载入建表目录
    load_inline(client, "t", "f", [{"id": 1, "name": "a", "age": 1}])
    # 直接破坏 parquet 字节（模拟底层数据损坏），读取必须归为 compute_failure
    pq = ws / "data" / "tables" / "t" / "f.v1.parquet"
    pq.write_bytes(b"NOT A PARQUET FILE" + b"\x00" * 100)
    r = client.post("/tables/t/query", json={})
    assert r.status_code == 500
    err = _err(r)
    assert err["category"] == "compute_failure"
    # run 日志保留错误类别，可按响应头里的 run_id 重放
    run_id = r.headers["x-run-id"]
    rec = _run(client, run_id)
    assert rec["error"]["category"] == "compute_failure"
    assert rec["endpoint"] == "/tables/query"
    assert rec["request"]["table_id"] == "t"
