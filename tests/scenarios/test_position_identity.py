"""位置删除的核心身份不变式（独立小测试，便于直接复核）。"""
from __future__ import annotations

from conftest import delete_batch, load_inline, snapshot, verdict_index


def test_old_row_numbers_not_reused_after_rewrite(client, make_table):
    # 5 行；先位置删除 rn=2（绑定 v1）；再等值删除 id=1；重写
    make_table("t")
    load_inline(client, "t", "f", [
        {"id": i, "name": f"n{i}", "age": i} for i in range(5)
    ])
    delete_batch(client, "t", [{"delete_id": "p2", "kind": "position",
                                "file_id": "f", "row_number": 2}])
    delete_batch(client, "t", [{"delete_id": "e0", "kind": "equality",
                                "key": {"id": 0}}])
    rw = client.post("/tables/t/rewrite", json={"file_ids": ["f"], "new_file_id": "g"})
    assert rw.status_code == 200
    body = rw.json()["result"]
    # 幸存 [1,3,4]，在 g 中行号 0,1,2，insert_seq 延续
    assert body["rows_written"] == 3

    snap = snapshot(client, "t")
    idx = verdict_index(snap)
    assert [idx[("g", rn)]["values"]["id"] for rn in range(3)] == [1, 3, 4]

    # 旧位置删除 p2 的评估状态必须是"失效-行已移除"，且 g 的 rn=2(id=4) 仍保留
    evals = {e["delete_id"]: e["status"] for e in snap["op_evaluations"]}
    assert evals["p2"] == "stale_row_already_removed"
    assert idx[("g", 2)]["action"] == "keep"
    # 等值删除 e0 在重写后仍成立（按行身份，不按行号）
    g_ids = {idx[("g", rn)]["values"]["id"] for rn in range(3)}
    assert 0 not in g_ids

    # 血缘侧录记录了每个新行的父行号
    lineage = (client.ws_path / "data" / "lineage" / "t" / "g.v1.json").read_text()
    import json
    src = json.loads(lineage)["source"]
    assert src["kind"] == "rewrite"
    assert [p[2] for p in src["parents"]] == [1, 3, 4]


def test_rewrite_preserves_row_numbers_of_survivors(client, make_table):
    make_table("t")
    load_inline(client, "t", "f", [
        {"id": i, "name": "n", "age": i} for i in range(4)
    ])
    # 删除最后一行 rn=3，幸存行 rn 不变（压缩不重排前缀行）
    delete_batch(client, "t", [{"delete_id": "p", "kind": "position",
                                "file_id": "f", "row_number": 3}])
    rw = client.post("/tables/t/rewrite", json={"file_ids": ["f"], "new_file_id": "g"})
    assert rw.status_code == 200
    import json
    parents = json.loads((client.ws_path / "data" / "lineage" / "t" / "g.v1.json")
                         .read_text())["source"]["parents"]
    assert [p[2] for p in parents] == [0, 1, 2]


def test_position_delete_then_equality_different_rows(client, make_table):
    make_table("t")
    load_inline(client, "t", "f", [
        {"id": 1, "name": "a", "age": 1},
        {"id": 1, "name": "dup", "age": 2},  # 同键不同行
    ])
    # 位置删除只删 rn=0；随后等值删除 id=1 会再删 rn=1
    delete_batch(client, "t", [{"delete_id": "p", "kind": "position",
                                "file_id": "f", "row_number": 0}])
    snap = snapshot(client, "t")
    idx = verdict_index(snap)
    assert idx[("f", 0)]["action"] == "delete"
    assert idx[("f", 1)]["action"] == "keep"
    delete_batch(client, "t", [{"delete_id": "e", "kind": "equality",
                                "key": {"id": 1}}])
    snap = snapshot(client, "t")
    idx = verdict_index(snap)
    assert idx[("f", 1)]["action"] == "delete"
    assert idx[("f", 1)]["by_delete_id"] == "e"


def test_merge_rewrite_multiple_files(client, make_table):
    make_table("t")
    load_inline(client, "t", "f", [{"id": 1, "name": "a", "age": 1}])
    load_inline(client, "t", "g", [{"id": 2, "name": "b", "age": 2}])
    rw = client.post("/tables/t/rewrite", json={"file_ids": ["f", "g"],
                                                "new_file_id": "m"})
    assert rw.status_code == 200
    snap = snapshot(client, "t")
    live = {x["file_id"] for x in snap["files"] if x["is_live"]}
    assert live == {"m"}
    idx = verdict_index(snap)
    assert [idx[("m", rn)]["values"]["id"] for rn in range(2)] == [1, 2]
