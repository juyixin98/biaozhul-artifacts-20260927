"""运行日志：run_id 可重放，关键中间状态与判断理由完整。"""
from __future__ import annotations

import json

from conftest import delete_batch, load_inline


def test_run_recorded_and_replayable(client, make_table):
    make_table("t")
    load_inline(client, "t", "f", [
        {"id": 1, "name": "a", "age": 1},
        {"id": 2, "name": "b", "age": 2},
    ])
    r = delete_batch(client, "t", [{"delete_id": "d1", "kind": "equality",
                                    "key": {"id": 1}}])
    run_id = r.json()["run_id"]

    rec = client.get(f"/runs/{run_id}").json()
    assert rec["run_id"] == run_id
    assert rec["endpoint"] == "/tables/deletes"
    assert rec["http_status"] == 200
    # 请求摘要可重放入参
    assert rec["request"]["deletes"][0]["delete_id"] == "d1"
    # 操作后状态：序列号水位、live 版本、删除清单
    st = rec["state_after"]
    assert st["seq_horizon"] == 2  # load=1, delete=2
    assert st["live_files"] == {"f": 1}
    assert [d["delete_id"] for d in st["deletes"]] == ["d1"]
    # 内核 trace 有逐行判断与理由
    traces = rec["kernel_trace"]
    del_traces = [t for t in traces if t.get("stage") == "row_verdict"
                  and t.get("action") == "delete"]
    assert len(del_traces) == 1
    assert del_traces[0]["reason"] == "equality_delete"
    assert del_traces[0]["by_delete_id"] == "d1"


def test_runs_index_lists_all(client, make_table):
    make_table("t")
    load_inline(client, "t", "f", [{"id": 1, "name": "a", "age": 1}])
    delete_batch(client, "t", [{"delete_id": "d", "kind": "equality",
                                "key": {"id": 1}}])
    listing = client.get("/runs").json()["runs"]
    endpoints = [x["endpoint"] for x in listing]
    assert "/tables/load" in endpoints and "/tables/deletes" in endpoints
    # 索引条目能取回完整记录
    rid = listing[0]["run_id"]
    assert client.get(f"/runs/{rid}").status_code == 200


def test_error_run_holds_category_and_state(client, make_table):
    make_table("t")
    load_inline(client, "t", "f", [{"id": 1, "name": "a", "age": 1}])
    r = delete_batch(client, "t", [{"delete_id": "d", "kind": "position",
                                    "file_id": "f", "row_number": 99}])
    assert r.status_code == 400
    run_id = r.headers["x-run-id"]
    rec = client.get(f"/runs/{run_id}").json()
    assert rec["error"]["category"] == "input_error"
    assert rec["error"]["details"]["row_count"] == 1
    assert rec["state_after"]["seq_horizon"] == 1  # 拒绝不发号
    # 落盘文件确实存在
    path = client.ws_path / "runs" / f"{run_id}.json"
    assert json.loads(path.read_text())["run_id"] == run_id


def test_trace_explains_late_insert(client, make_table):
    """先删后插：trace 必须记录越界保留的理由（insert_seq > delete_seq）。"""
    make_table("t")
    load_inline(client, "t", "f", [{"id": 5, "name": "a", "age": 1}])
    delete_batch(client, "t", [{"delete_id": "d5", "kind": "equality",
                                "key": {"id": 5}}])
    load_inline(client, "t", "g", [{"id": 5, "name": "new", "age": 2}])
    r = client.post("/tables/t/query", json={})
    run_id = r.json()["run_id"]
    rec = client.get(f"/runs/{run_id}").json()
    oos = [t for t in rec["kernel_trace"] if t.get("stage") == "out_of_scope_keep"]
    assert len(oos) == 1
    assert oos[0]["delete_id"] == "d5"
    assert oos[0]["insert_seq"] > oos[0]["delete_seq"]
    assert "insert_seq > delete_seq" in oos[0]["rule"]
