"""逐版本事件与运行日志测试：事件顺序、可重放中间状态、失败分类留痕。"""
from __future__ import annotations


def _commit(client, tid, parent, ops):
    r = client.post(f"/tables/{tid}/commits",
                    json={"table_id": tid, "parent_snapshot_id": parent, "operations": ops})
    assert r.status_code == 200, r.text
    return r.json()


def test_event_log_per_version(client):
    r = client.post("/tables", json={
        "name": "ev",
        "columns": [{"name": "id", "type": "long"}, {"name": "n", "type": "string"}],
        "primary_key": ["id"]})
    tid = r.json()["table_id"]
    create_run = r.json()["run_id"]

    s1 = _commit(client, tid, None, [{"op": "append", "ref": "f", "rows": [
        {"id": 1, "n": "a"}, {"id": 2, "n": "b"}]}])
    fid = s1["files"][0]["file_id"]
    s2 = _commit(client, tid, s1["snapshot_id"], [
        {"op": "position_delete", "target_file": fid, "positions": [0]}])
    s3 = _commit(client, tid, s2["snapshot_id"], [
        {"op": "rewrite", "ref": "g", "drops": [fid], "rows": [{"id": 9, "n": "z"}]}])

    events = client.get(f"/tables/{tid}/events").json()["events"]
    types = [(e["seq"], e["event_type"]) for e in events]
    assert types == [
        (None, "TABLE_CREATED"),
        (1, "DATA_FILE_ADDED"),
        (1, "SNAPSHOT_COMMITTED"),
        (2, "POSITION_DELETE_ADDED"),
        (2, "SNAPSHOT_COMMITTED"),
        (3, "DATA_FILE_REWRITTEN"),
        (3, "SNAPSHOT_COMMITTED"),
    ]
    # 重写事件载荷记录被替换文件
    rewrite = [e for e in events if e["event_type"] == "DATA_FILE_REWRITTEN"][0]
    assert rewrite["payload"]["drops"] == [fid]

    # 事件可按 seq 裁剪（逐版本参考）
    at_v2 = client.get(f"/tables/{tid}/events", params={"seq": 2}).json()["events"]
    assert max(e["seq"] or 0 for e in at_v2 if e["event_type"] != "TABLE_CREATED") <= 2
    assert not any(e["event_type"] == "DATA_FILE_REWRITTEN" for e in at_v2)


def test_run_log_records_phases_and_rationales(client):
    r = client.post("/tables", json={
        "name": "rl",
        "columns": [{"name": "id", "type": "long"}], "primary_key": ["id"]})
    tid = r.json()["table_id"]
    s1 = _commit(client, tid, None, [{"op": "append", "ref": "f", "rows": [{"id": 1}, {"id": 2}]}])
    fid = s1["files"][0]["file_id"]
    c2 = _commit(client, tid, s1["snapshot_id"],
                 [{"op": "position_delete", "target_file": fid, "positions": [1]}])

    run = client.get(f"/runs/{c2['run_id']}").json()
    assert run["kind"] == "COMMIT" and run["status"] == "OK"
    phases = [p["phase"] for p in run["phases"]]
    assert "operations_parsed" in phases
    assert "state_validated" in phases
    assert "data_file_written" not in phases
    assert "position_delete_written" in phases
    assert "metadata_committed" in phases
    state = next(p for p in run["phases"] if p["phase"] == "state_validated")
    assert state["detail"]["next_seq"] == 2

    # explain 运行日志记录中间状态（列裁剪集合、文件计数）
    ex = client.post(f"/tables/{tid}/explain", json={"columns": ["id"]})
    ex_run = client.get(f"/runs/{ex.json()['run_id']}").json()
    scan = next(p for p in ex_run["phases"] if p["phase"] == "scan_complete")
    assert scan["detail"]["live_file_count"] == 1
    assert "id" in scan["detail"]["read_columns"]


def test_failed_commit_rolls_back_and_is_replayable(client):
    r = client.post("/tables", json={
        "name": "rb",
        "columns": [{"name": "id", "type": "long"}], "primary_key": ["id"]})
    tid = r.json()["table_id"]
    s1 = _commit(client, tid, None, [{"op": "append", "ref": "f", "rows": [{"id": 1}]}])
    before = client.get(f"/tables/{tid}/snapshots").json()["snapshots"]

    bad = client.post(f"/tables/{tid}/commits", json={
        "table_id": tid, "parent_snapshot_id": s1["snapshot_id"],
        "operations": [{"op": "position_delete", "target_file": "file-ghost", "positions": [0]}]})
    assert bad.status_code == 409
    run = client.get(f"/runs/{bad.json()['run_id']}").json()
    assert run["status"] == "ERROR" and run["error"]["code"] == "POSITION_TARGET_UNKNOWN"

    # 无新快照、序列号未被消耗：下一次正常提交仍是 seq=2
    after = client.get(f"/tables/{tid}/snapshots").json()["snapshots"]
    assert len(after) == len(before)
    ok = _commit(client, tid, s1["snapshot_id"],
                 [{"op": "append", "ref": "g", "rows": [{"id": 2}]}])
    assert ok["seq"] == 2
