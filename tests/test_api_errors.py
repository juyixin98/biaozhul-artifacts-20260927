"""错误分类测试：输入错误 / 状态冲突 / 资源耗尽 / 计算失败必须可区分。

每个用例同时断言 HTTP 状态、error.category 与 error.code，
并检查运行日志记录了 run_id 与失败类别（可重放）。
"""
from __future__ import annotations

import json

import pytest


def _make_table(client, config=None, rows=None):
    r = client.post("/tables", json={
        "name": "err",
        "columns": [{"name": "id", "type": "long"}, {"name": "name", "type": "string"}],
        "primary_key": ["id"], "config": config,
    })
    assert r.status_code == 200
    tid = r.json()["table_id"]
    if rows:
        r = client.post(f"/tables/{tid}/commits", json={
            "table_id": tid, "parent_snapshot_id": None,
            "operations": [{"op": "append", "ref": "f", "rows": rows}]})
        assert r.status_code == 200
        return tid, r.json()
    return tid, None


def _err(client, tid, parent, ops, status, category, code):
    r = client.post(f"/tables/{tid}/commits",
                    json={"table_id": tid, "parent_snapshot_id": parent, "operations": ops})
    assert r.status_code == status, (status, r.text)
    body = r.json()
    assert body["error"]["category"] == category, body
    assert body["error"]["code"] == code, body
    run = client.get(f"/runs/{body['run_id']}").json()
    assert run["status"] == "ERROR"
    assert run["error"]["category"] == category
    assert run["error"]["code"] == code
    return body


# ---- 输入错误 VALIDATION_ERROR (400) --------------------------------------
def test_unknown_op_is_validation_error(client):
    tid, _ = _make_table(client)
    _err(client, tid, None, [{"op": "merge", "rows": []}], 400, "VALIDATION_ERROR", "UNKNOWN_OP")


def test_empty_commit_rejected(client):
    tid, _ = _make_table(client)
    _err(client, tid, None, [], 400, "VALIDATION_ERROR", "EMPTY_COMMIT")


def test_type_mismatch_validation_error(client):
    tid, _ = _make_table(client)
    _err(client, tid, None, [{"op": "append", "ref": "f", "rows": [{"id": "x", "name": "a"}]}],
         400, "VALIDATION_ERROR", "TYPE_MISMATCH")


def test_unknown_column_in_row(client):
    tid, _ = _make_table(client)
    _err(client, tid, None, [{"op": "append", "ref": "f",
                              "rows": [{"id": 1, "name": "a", "ghost": 1}]}],
         400, "VALIDATION_ERROR", "UNKNOWN_COLUMN")


def test_position_out_of_range_is_input_error(client):
    tid, s = _make_table(client, rows=[{"id": 1, "name": "a"}])
    fid = s["files"][0]["file_id"]
    _err(client, tid, s["snapshot_id"],
         [{"op": "position_delete", "target_file": fid, "positions": [5]}],
         400, "VALIDATION_ERROR", "POSITION_OUT_OF_RANGE")


def test_duplicate_position_in_batch(client):
    tid, s = _make_table(client, rows=[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])
    fid = s["files"][0]["file_id"]
    _err(client, tid, s["snapshot_id"],
         [{"op": "position_delete", "target_file": fid, "positions": [0, 0]}],
         400, "VALIDATION_ERROR", "DUPLICATE_POSITION")


def test_position_target_same_commit_rejected(client):
    tid, _ = _make_table(client)
    _err(client, tid, None, [
        {"op": "append", "ref": "f", "rows": [{"id": 1, "name": "a"}]},
        {"op": "position_delete", "target_file": "f", "positions": [0]},
    ], 400, "VALIDATION_ERROR", "POSITION_TARGET_SAME_COMMIT")


def test_missing_key_value_requires_explicit_null(client):
    tid, s = _make_table(client, rows=[{"id": 1, "name": "a"}])
    _err(client, tid, s["snapshot_id"],
         [{"op": "equality_delete", "predicates": [{"key": {"name": "a"}}]}],
         400, "VALIDATION_ERROR", "MISSING_KEY_VALUE")


def test_duplicate_ref(client):
    tid, _ = _make_table(client)
    _err(client, tid, None, [
        {"op": "append", "ref": "f", "rows": [{"id": 1, "name": "a"}]},
        {"op": "append", "ref": "f", "rows": [{"id": 2, "name": "b"}]},
    ], 400, "VALIDATION_ERROR", "DUPLICATE_REF")


def test_rewrite_without_drops(client):
    tid, s = _make_table(client, rows=[{"id": 1, "name": "a"}])
    _err(client, tid, s["snapshot_id"],
         [{"op": "rewrite", "ref": "g", "drops": [], "rows": [{"id": 2, "name": "b"}]}],
         400, "VALIDATION_ERROR", "REWRITE_REQUIRES_DROPS")


def test_validate_endpoint_reports_same_errors_as_commit(client):
    tid, s = _make_table(client, rows=[{"id": 1, "name": "a"}])
    fid = s["files"][0]["file_id"]
    body = {"table_id": tid, "parent_snapshot_id": s["snapshot_id"],
            "operations": [{"op": "position_delete", "target_file": fid, "positions": [9]}]}
    r1 = client.post(f"/tables/{tid}/validate", json=body)
    r2 = client.post(f"/tables/{tid}/commits", json=body)
    assert r1.status_code == r2.status_code == 400
    assert r1.json()["error"]["code"] == r2.json()["error"]["code"] == "POSITION_OUT_OF_RANGE"
    # validate 不得产生任何文件或快照
    assert len(client.get(f"/tables/{tid}/snapshots").json()["snapshots"]) == 1


def test_filter_unknown_column_and_bad_dsl(client):
    tid, s = _make_table(client, rows=[{"id": 1, "name": "a"}])
    r = client.get(f"/tables/{tid}/rows",
                   params={"filter": json.dumps({"column": "ghost", "op": "=", "value": 1})})
    assert r.status_code == 400 and r.json()["error"]["code"] == "UNKNOWN_FILTER_COLUMN"
    r = client.post(f"/tables/{tid}/explain", json={"filter": {"op": "=", "value": 1}})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_FILTER"


# ---- 状态冲突 STATE_CONFLICT (409) ----------------------------------------
def test_parent_mismatch_concurrency_conflict(client):
    tid, s = _make_table(client, rows=[{"id": 1, "name": "a"}])
    fid = s["files"][0]["file_id"]
    # 第一个提交成功推进快照
    ok = client.post(f"/tables/{tid}/commits", json={
        "table_id": tid, "parent_snapshot_id": s["snapshot_id"],
        "operations": [{"op": "position_delete", "target_file": fid, "positions": [0]}]})
    assert ok.status_code == 200
    # 仍以旧父快照提交 -> 409
    _err(client, tid, s["snapshot_id"],
         [{"op": "append", "ref": "g", "rows": [{"id": 2, "name": "b"}]}],
         409, "STATE_CONFLICT", "PARENT_MISMATCH")


def test_initial_commit_with_parent_conflicts(client):
    tid, _ = _make_table(client)
    _err(client, tid, "snap-doesnotexist",
         [{"op": "append", "ref": "f", "rows": [{"id": 1, "name": "a"}]}],
         409, "STATE_CONFLICT", "PARENT_MISMATCH")


def test_noninitial_commit_requires_parent(client):
    tid, s = _make_table(client, rows=[{"id": 1, "name": "a"}])
    _err(client, tid, None, [{"op": "append", "ref": "g", "rows": [{"id": 2, "name": "b"}]}],
         409, "STATE_CONFLICT", "PARENT_REQUIRED")


def test_rewrite_then_stale_position_is_state_conflict(client):
    tid, s = _make_table(client, rows=[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])
    fid = s["files"][0]["file_id"]
    s2 = client.post(f"/tables/{tid}/commits", json={
        "table_id": tid, "parent_snapshot_id": s["snapshot_id"],
        "operations": [{"op": "rewrite", "ref": "g", "drops": [fid],
                        "rows": [{"id": 9, "name": "z"}]}]})
    assert s2.status_code == 200
    _err(client, tid, s2.json()["snapshot_id"],
         [{"op": "position_delete", "target_file": fid, "positions": [0]}],
         409, "STATE_CONFLICT", "STALE_POSITION_TARGET")


def test_drop_nonlive_file_conflict(client):
    tid, s = _make_table(client, rows=[{"id": 1, "name": "a"}])
    fid = s["files"][0]["file_id"]
    s2 = client.post(f"/tables/{tid}/commits", json={
        "table_id": tid, "parent_snapshot_id": s["snapshot_id"],
        "operations": [{"op": "rewrite", "ref": "g", "drops": [fid],
                        "rows": [{"id": 2, "name": "b"}]}]})
    assert s2.status_code == 200
    _err(client, tid, s2.json()["snapshot_id"],
         [{"op": "rewrite", "ref": "h", "drops": [fid], "rows": [{"id": 3, "name": "c"}]}],
         409, "STATE_CONFLICT", "FILE_NOT_LIVE")


# ---- 资源耗尽 RESOURCE_EXHAUSTED (413) ------------------------------------
def test_row_limit_resource_exhausted(client):
    tid, _ = _make_table(client, config={"max_rows_per_data_file": 2})
    _err(client, tid, None, [{"op": "append", "ref": "f", "rows": [
        {"id": 1, "name": "a"}, {"id": 2, "name": "b"}, {"id": 3, "name": "c"}]}],
         413, "RESOURCE_EXHAUSTED", "TOO_MANY_ROWS")


def test_files_limit_resource_exhausted(client):
    tid, _ = _make_table(client, config={"max_files_per_snapshot": 1})
    _err(client, tid, None, [
        {"op": "append", "ref": "f", "rows": [{"id": 1, "name": "a"}]},
        {"op": "append", "ref": "g", "rows": [{"id": 2, "name": "b"}]},
    ], 413, "RESOURCE_EXHAUSTED", "TOO_MANY_FILES")


# ---- 计算失败 / 完整性 COMPUTATION_FAILED (500) ---------------------------
def test_tampered_data_file_is_computation_failure(client):
    tid, s = _make_table(client, rows=[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])
    files = client.get(f"/tables/{tid}/files").json()["live_files"]
    path = client.app.state.config.warehouse_dir / files[0]["path"]
    with open(path, "ab") as f:
        f.write(b"corruption")
    r = client.get(f"/tables/{tid}/rows")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["category"] == "COMPUTATION_FAILED"
    assert body["error"]["code"] == "CONTENT_HASH_MISMATCH"
    run = client.get(f"/runs/{body['run_id']}").json()
    assert run["error"]["code"] == "CONTENT_HASH_MISMATCH"


def test_missing_table_is_not_found(client):
    r = client.get("/tables/tbl-nope/rows")
    assert r.status_code == 404 and r.json()["error"]["category"] == "NOT_FOUND"


def test_missing_run_is_not_found(client):
    r = client.get("/runs/run-nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_error_envelope_shape(client):
    r = client.post("/tables/tbl-missing/commits",
                    json={"table_id": "tbl-missing", "operations": []})
    body = r.json()
    assert set(body) == {"error", "run_id"}
    assert set(body["error"]) == {"category", "code", "message", "details"}
    assert body["run_id"].startswith("run-")
