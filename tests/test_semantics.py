"""聚焦语义的单元/集成测试：断言具体结果而非“接口可调”。"""
from __future__ import annotations

import json

import pytest


def _create(client, *, pk=("id",), extra_types=None):
    cols = [
        {"name": "id", "type": "long"},
        {"name": "name", "type": "string"},
        {"name": "age", "type": "int"},
        {"name": "d", "type": "date"},
    ]
    if extra_types:
        cols += extra_types
    r = client.post("/tables", json={
        "name": "sem", "columns": cols, "primary_key": list(pk),
    })
    assert r.status_code == 200, r.text
    return r.json()["table_id"]


def _commit(client, tid, parent, ops, expected=200):
    r = client.post(f"/tables/{tid}/commits",
                    json={"table_id": tid, "parent_snapshot_id": parent, "operations": ops})
    assert r.status_code == expected, r.text
    return r


def _live_ids(client, tid, **params):
    r = client.get(f"/tables/{tid}/rows", params=params)
    assert r.status_code == 200, r.text
    return sorted(row["id"] for row in r.json()["rows"])


def test_position_delete_is_identity_bound_and_rewrite_rejects_stale_lines(client):
    tid = _create(client)
    s1 = _commit(client, tid, None, [{"op": "append", "ref": "f", "rows": [
        {"id": i, "name": f"n{i}", "age": i, "d": "2026-01-01"} for i in range(5)]}]).json()
    fid = s1["files"][0]["file_id"]

    s2 = _commit(client, tid, s1["snapshot_id"],
                 [{"op": "position_delete", "target_file": fid, "positions": [0, 4]}]).json()
    assert _live_ids(client, tid) == [1, 2, 3]

    # 重写：旧文件被 DROP，新文件从行号 0 重新开始
    s3 = _commit(client, tid, s2["snapshot_id"],
                 [{"op": "rewrite", "ref": "g", "drops": [fid], "rows": [
                     {"id": 9, "name": "nine", "age": 9, "d": "2026-02-02"}]}]).json()
    gid = s3["files"][0]["file_id"]
    assert _live_ids(client, tid) == [9]

    # 旧行号不能作用到新文件
    r = _commit(client, tid, s3["snapshot_id"],
                [{"op": "position_delete", "target_file": fid, "positions": [1]}], expected=409)
    assert r.json()["error"]["category"] == "STATE_CONFLICT"
    assert r.json()["error"]["code"] == "STALE_POSITION_TARGET"

    # 新文件自己的行号可用
    r = _commit(client, tid, s3["snapshot_id"],
                [{"op": "position_delete", "target_file": gid, "positions": [0]}])
    assert r.status_code == 200
    assert _live_ids(client, tid) == []


def test_equality_delete_sequence_window_delete_then_insert(client):
    tid = _create(client)
    s1 = _commit(client, tid, None, [{"op": "append", "ref": "f", "rows": [
        {"id": 1, "name": "a", "age": 1, "d": "2026-01-01"},
        {"id": 2, "name": "b", "age": 2, "d": "2026-01-02"}]}]).json()
    s2 = _commit(client, tid, s1["snapshot_id"],
                 [{"op": "equality_delete", "predicates": [{"key": {"id": 2}}]}]).json()
    assert _live_ids(client, tid) == [1]

    # 同键后插入：新文件 added_seq=3，删除 seq=2，窗口外 -> 存活
    s3 = _commit(client, tid, s2["snapshot_id"],
                 [{"op": "append", "ref": "g", "rows": [
                     {"id": 2, "name": "b2", "age": 20, "d": "2026-03-02"}]}]).json()
    assert _live_ids(client, tid) == [1, 2]

    # 新删除 seq=4 命中新文件
    s4 = _commit(client, tid, s3["snapshot_id"],
                 [{"op": "equality_delete", "predicates": [{"key": {"id": 2}}]}]).json()
    assert _live_ids(client, tid) == [1]

    # explain 中新文件被删的理由只含 seq=4（不含 seq=2 —— 序列号窗口证据）
    ex = client.post(f"/tables/{tid}/explain", json={}).json()
    deleted = {(r["file_id"], r["position"]): r for r in ex["rows"] if r["disposition"] == "DELETED"}
    gid = s3["files"][0]["file_id"]
    assert [(x["kind"], x["seq"]) for x in deleted[(gid, 0)]["reasons"]] == [("EQUALITY", 4)]


def test_null_key_delete_matches_nothing_including_null_rows(client):
    tid = _create(client)
    s1 = _commit(client, tid, None, [
        {"op": "append", "ref": "f", "rows": [
            {"id": None, "name": "nullrow", "age": 1, "d": None},
            {"id": 1, "name": "one", "age": 2, "d": "2026-01-01"}]},
    ]).json()
    # 后续提交：删除向量含显式 NULL 键，按 SQL NULL 语义不命中任何行（NULL 数据行也存活）
    s2 = _commit(client, tid, s1["snapshot_id"], [
        {"op": "equality_delete", "predicates": [{"key": {"id": None}}]},
    ]).json()
    ex = client.post(f"/tables/{tid}/explain", json={}).json()
    null_rows = [r for r in ex["rows"] if r["row"]["id"] is None]
    assert len(null_rows) == 1 and null_rows[0]["disposition"] == "KEPT"
    one = [r for r in ex["rows"] if r["row"]["id"] == 1]
    assert one[0]["disposition"] == "KEPT"
    assert ex["null_keys_ignored"] == [{"delete_file_id": ex["delete_files"][0]["delete_file_id"],
                                        "seq": 2, "count": 1}]


def test_duplicate_keys_all_old_rows_deleted_but_not_new_file(client):
    tid = _create(client)
    s1 = _commit(client, tid, None, [
        {"op": "append", "ref": "f", "rows": [
            {"id": 7, "name": "x", "age": 1, "d": "2026-01-01"},
            {"id": 7, "name": "y", "age": 2, "d": "2026-01-02"}]},
        {"op": "append", "ref": "g", "rows": [
            {"id": 7, "name": "z", "age": 3, "d": "2026-01-03"},
            {"id": 8, "name": "q", "age": 4, "d": "2026-01-04"}]},
    ]).json()
    fid, gid = (x["file_id"] for x in s1["files"])
    s2 = _commit(client, tid, s1["snapshot_id"],
                 [{"op": "equality_delete", "predicates": [{"key": {"id": 7}}]}]).json()
    assert _live_ids(client, tid) == [8]
    s3 = _commit(client, tid, s2["snapshot_id"],
                 [{"op": "append", "ref": "h", "rows": [
                     {"id": 7, "name": "new7", "age": 5, "d": "2026-01-05"}]}]).json()
    assert _live_ids(client, tid) == [7, 8]
    ex = client.post(f"/tables/{tid}/explain", json={}).json()
    new_deleted = [r for r in ex["rows"]
                   if r["file_id"] == s3["files"][0]["file_id"] and r["disposition"] == "DELETED"]
    assert new_deleted == []


def test_cross_file_position_and_equality(client):
    tid = _create(client)
    s1 = _commit(client, tid, None, [
        {"op": "append", "ref": "f", "rows": [{"id": 1, "name": "a", "age": 1, "d": "2026-01-01"}]},
        {"op": "append", "ref": "g", "rows": [
            {"id": 2, "name": "b", "age": 2, "d": "2026-01-02"},
            {"id": 3, "name": "c", "age": 3, "d": "2026-01-03"}]},
    ]).json()
    fid, gid = s1["files"][0]["file_id"], s1["files"][1]["file_id"]
    s2 = _commit(client, tid, s1["snapshot_id"], [
        {"op": "position_delete", "target_file": gid, "positions": [1]},
        {"op": "equality_delete", "predicates": [{"key": {"id": 1}}]},
    ]).json()
    assert _live_ids(client, tid) == [2]
    ex = client.post(f"/tables/{tid}/explain", json={}).json()
    by_pos = {(r["file_id"], r["position"]): r for r in ex["rows"]}
    assert by_pos[(fid, 0)]["disposition"] == "DELETED"
    assert by_pos[(gid, 0)]["disposition"] == "KEPT"
    assert by_pos[(gid, 1)]["disposition"] == "DELETED"


def test_filter_and_projection_do_not_change_delete_semantics(client):
    tid = _create(client)
    s1 = _commit(client, tid, None, [{"op": "append", "ref": "f", "rows": [
        {"id": i, "name": f"n{i}", "age": i * 10, "d": "2026-01-01"} for i in range(1, 6)]}]).json()
    fid = s1["files"][0]["file_id"]
    _commit(client, tid, s1["snapshot_id"], [{"op": "position_delete", "target_file": fid, "positions": [3]}])
    # 投影不含 id 与 age 以外的键语义无关；先删后过滤：age>=40 中 id=4 已删，只剩 id=5
    r = client.get(f"/tables/{tid}/rows",
                   params={"columns": "name", "filter": json.dumps({"column": "age", "op": ">=", "value": 40})})
    assert r.status_code == 200
    assert [row["name"] for row in r.json()["rows"]] == ["n5"]
    assert set(r.json()["rows"][0].keys()) == {"name"}

    # explain 中被删行即使满足过滤也仍为 DELETED，不会复活成 KEPT
    ex = client.post(f"/tables/{tid}/explain",
                     json={"filter": {"column": "age", "op": ">=", "value": 40}}).json()
    disp = {(r["position"]): r["disposition"] for r in ex["rows"]}
    assert disp[3] == "DELETED"
    assert disp[4] == "KEPT"
    assert all(disp[i] == "FILTERED" for i in (0, 1, 2))


def test_composite_primary_key_null_semantics(client):
    r = client.post("/tables", json={
        "name": "compound",
        "columns": [{"name": "a", "type": "long"}, {"name": "b", "type": "string"},
                    {"name": "v", "type": "int"}],
        "primary_key": ["a", "b"],
    })
    tid = r.json()["table_id"]
    s1 = _commit(client, tid, None, [{"op": "append", "ref": "f", "rows": [
        {"a": 1, "b": "x", "v": 10}, {"a": 1, "b": "y", "v": 20},
        {"a": 2, "b": "x", "v": 30}, {"a": None, "b": "x", "v": 40}]}]).json()
    _commit(client, tid, s1["snapshot_id"],
            [{"op": "equality_delete", "predicates": [{"key": {"a": 1, "b": "x"}}]}])
    rows = client.get(f"/tables/{tid}/rows").json()["rows"]
    assert sorted((r["a"], r["b"]) for r in rows if r["a"] is not None) == [(1, "y"), (2, "x")]
    assert [r for r in rows if r["a"] is None] == [{"a": None, "b": "x", "v": 40}]
    # 部分 NULL 键删除不命中（后续提交，序列号窗口满足，但 NULL 不匹配）
    s2 = client.get(f"/tables/{tid}/snapshots").json()["snapshots"][-1]["snapshot_id"]
    _commit(client, tid, s2, [{"op": "equality_delete",
                              "predicates": [{"key": {"a": None, "b": "x"}}]}])
    rows = client.get(f"/tables/{tid}/rows").json()["rows"]
    assert sorted((r["a"], r["b"]) for r in rows if r["a"] is not None) == [(1, "y"), (2, "x")]
    assert [r for r in rows if r["a"] is None] == [{"a": None, "b": "x", "v": 40}]


def test_time_travel_per_version(client):
    tid = _create(client)
    s1 = _commit(client, tid, None, [{"op": "append", "ref": "f", "rows": [
        {"id": 1, "name": "a", "age": 1, "d": "2026-01-01"}]}]).json()
    s2 = _commit(client, tid, s1["snapshot_id"],
                 [{"op": "equality_delete", "predicates": [{"key": {"id": 1}}]}]).json()
    # 当前为空
    assert client.get(f"/tables/{tid}/rows").json()["rows"] == []
    # 按 snapshot_id 与 seq 回读 v1
    r1 = client.get(f"/tables/{tid}/rows", params={"snapshot_id": s1["snapshot_id"]})
    assert [x["id"] for x in r1.json()["rows"]] == [1]
    r1b = client.get(f"/tables/{tid}/rows", params={"seq": 1})
    assert [x["id"] for x in r1b.json()["rows"]] == [1]
    assert r1.json()["snapshot_id"] == s1["snapshot_id"]


def test_position_and_equality_both_apply_to_same_row(client):
    tid = _create(client)
    s1 = _commit(client, tid, None, [{"op": "append", "ref": "f", "rows": [
        {"id": 1, "name": "a", "age": 1, "d": "2026-01-01"},
        {"id": 2, "name": "b", "age": 2, "d": "2026-01-02"}]}]).json()
    fid = s1["files"][0]["file_id"]
    _commit(client, tid, s1["snapshot_id"], [
        {"op": "position_delete", "target_file": fid, "positions": [0]},
        {"op": "equality_delete", "predicates": [{"key": {"id": 1}}]},
    ])
    ex = client.post(f"/tables/{tid}/explain", json={}).json()
    r0 = next(r for r in ex["rows"] if r["position"] == 0)
    kinds = sorted((x["kind"], x["seq"]) for x in r0["reasons"])
    assert kinds == [("EQUALITY", 2), ("POSITION", 2)]


def test_boolean_and_double_strict_typing(client):
    r = client.post("/tables", json={
        "name": "typed",
        "columns": [{"name": "id", "type": "long"}, {"name": "flag", "type": "boolean"},
                    {"name": "ratio", "type": "double"}],
        "primary_key": ["id"],
    })
    tid = r.json()["table_id"]
    # bool 列拒绝 0/1
    bad = _commit(client, tid, None, [{"op": "append", "ref": "f", "rows": [
        {"id": 1, "flag": 1, "ratio": 0.5}]}], expected=400)
    assert bad.json()["error"]["code"] == "TYPE_MISMATCH"
    good = _commit(client, tid, None, [{"op": "append", "ref": "f", "rows": [
        {"id": 1, "flag": True, "ratio": 0.5}, {"id": 2, "flag": False, "ratio": 1.25}]}])
    assert good.status_code == 200
    rows = client.get(f"/tables/{tid}/rows").json()["rows"]
    assert rows[0]["flag"] is True and rows[0]["ratio"] == 0.5
