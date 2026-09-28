"""运行日志测试：运行编号、中间状态、判断理由足以重放问题。"""
from __future__ import annotations

import json

from merge_engine import MergeRequest

from conftest import cfg, seed


def test_journal_records_pipeline_with_reason_and_run_id(tmp_path):
    eng = seed(tmp_path, "j1", ["k1", "k2", "v"], [
        {"k1": "a", "k2": 1, "v": 1},
    ])
    req = MergeRequest(
        source={"format": "records", "records": [{"k1": "a", "k2": 1, "v": 2}]},
        config=cfg("j1", ("k1", "k2")),
    )
    result = eng.run(req)
    events = eng.journal.read_events(result.run_id)
    assert events, "no journal events"
    # 全部事件共享同一 run_id 且有单调时间戳/阶段
    assert all(e["run_id"] == result.run_id for e in events)
    names = [e["event"] for e in events]
    assert names[0] == "run_started"
    assert "source_loaded" in names
    assert "snapshot_loaded" in names
    assert "plan_decided" in names
    assert "plan_validated" in names
    assert names[-1] == "run_committed"

    # 关键中间状态：操作前快照 rowid 与指纹
    snap = next(e for e in events if e["event"] == "snapshot_loaded")
    assert snap["snapshot_rowids"] == [1]
    decided = next(e for e in events if e["event"] == "plan_decided")
    assert decided["fingerprint"] == result.plan.snapshot_fingerprint
    # 动作理由被逐条记录
    assert decided["actions"][0]["reason"] == "MATCHED_UPDATE_COND_TRUE"
    assert decided["actions"][0]["type"] == "UPDATE_MATCHED"


def test_journal_replay_for_rejected_run(tmp_path):
    eng = seed(tmp_path, "j2", ["k1", "k2"], [])
    req = MergeRequest(
        source={"format": "records", "records": [
            {"k1": "a", "k2": 1}, {"k1": "a", "k2": 1},
        ]},
        config=cfg("j2", ("k1", "k2")),
    )
    result = eng.run(req)
    events = eng.journal.read_events(result.run_id)
    rej = next(e for e in events if e["event"] == "run_rejected")
    assert rej["phase"] == "plan"
    assert rej["error"]["code"] == "SOURCE_DUPLICATE_KEY"
    assert rej["error"]["details"]["duplicates"][0]["rownums"] == [1, 2]


def test_journal_replay_for_failed_commit(tmp_path):
    eng = seed(tmp_path, "j3", ["k1", "k2", "v"], [
        {"k1": "a", "k2": 1, "v": 1},
    ])
    req = MergeRequest(
        source={"format": "records", "records": [{"k1": "a", "k2": 1, "v": 2}]},
        config=cfg("j3", ("k1", "k2")), fault_point="before_commit",
    )
    result = eng.run(req)
    events = eng.journal.read_events(result.run_id)
    assert [e["event"] for e in events].count("commit_failed") == 1
    failed = next(e for e in events if e["event"] == "commit_failed")
    assert failed["error"]["code"] == "DISK_FULL"
    # 回滚后用日志里的指纹可核对：重新运行的操作前快照指纹相同
    assert failed["snapshot_fingerprint"] == result.plan.snapshot_fingerprint
    # 日志文件是逐行 JSON，可被独立工具重放解析
    path = eng.journal.path_for(result.run_id)
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    assert all(json.loads(line)["run_id"] == result.run_id for line in raw_lines)


def test_each_run_has_distinct_id(tmp_path):
    eng = seed(tmp_path, "j4", ["k1", "k2", "v"], [])
    req = MergeRequest(
        source={"format": "records", "records": [{"k1": "a", "k2": 1, "v": 1}]},
        config=cfg("j4", ("k1", "k2")),
    )
    ids = {eng.run(req).run_id for _ in range(3)}
    assert len(ids) == 3
