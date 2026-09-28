#!/usr/bin/env python3
"""端到端本地演示：用合成夹具跑第三阶段的全部关键场景并打印人工核验表。

运行：
    PYTHONPATH=src .venv/bin/python scripts/demo.py

输出：每个场景的 run_id、动作集合（类型/键/理由）、目标前后对照、
故障注入后的回滚核验，以及 JSONL 日志文件路径。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from merge_engine import MergeEngine, MergeRequest  # noqa: E402
from merge_engine import store  # noqa: E402


def banner(title):
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def show(result, eng, table):
    print(f"run_id      : {result.run_id}")
    print(f"status      : {result.status}")
    if result.error:
        print(f"error       : [{result.error['category']}] {result.error['code']}")
    print("actions:")
    for a in (result.plan.actions if result.plan else []):
        key = json.dumps(a.key, ensure_ascii=False)
        print(f"  - {a.type.value:17s} key={key:<24s} reason={a.reason}"
              + (f" src_row={a.source_rownum}" if a.source_rownum else "")
              + (f" tgt_rowid={a.target_rowid}" if a.target_rowid else ""))
    if result.counts:
        print(f"write_counts: {result.counts}")
    print("target now  :", json.dumps(eng.get_target_rows(table), ensure_ascii=False))
    print(f"journal     : {eng.journal.path_for(result.run_id)}")


def main():
    root = tempfile.mkdtemp(prefix="merge-demo-")
    db = os.path.join(root, "merge.db")
    journal = os.path.join(root, "journal")
    print("workdir:", root)

    # ---- 场景 1：复合键全动作集合 ----------------------------------------
    banner("场景 1：复合键 [region,id] —— 匹配更新 / 未匹配插入 / 条件删除")
    eng = MergeEngine(db, journal)
    conn = store.connect(db)
    store.ensure_meta(conn)
    store.seed_target(conn, "items", ["region", "id", "v", "status"], [
        {"region": "us", "id": 1, "v": 10, "status": "OK"},
        {"region": "us", "id": 2, "v": 20, "status": "STALE"},
        {"region": "eu", "id": 9, "v": 30, "status": "KEEP"},
    ])
    conn.commit(); conn.close()
    res = eng.run(MergeRequest(
        source={"format": "records", "records": [
            {"region": "us", "id": 1, "v": 11, "status": "OK"},   # 更新
            {"region": "us", "id": 3, "v": 5, "status": "OK"},    # 插入（v>=5）
            {"region": "us", "id": 4, "v": 1, "status": "OK"},    # 插入被拦
        ]},
        config={
            "target_table": "items", "key_columns": ["region", "id"],
            "insert_when": {"op": "gte",
                            "left": {"side": "source", "column": "v"},
                            "right": {"literal": 5}},
            "delete_unmatched": True,
            "delete_when": {"op": "eq",
                            "left": {"side": "target", "column": "status"},
                            "right": {"literal": "STALE"}},
        }))
    show(res, eng, "items")
    assert res.status == "COMMITTED"

    # ---- 场景 2：源重复键 -------------------------------------------------
    banner("场景 2：源内同键多行 -> INPUT_ERROR/SOURCE_DUPLICATE_KEY（与行序无关）")
    res = eng.run(MergeRequest(
        source={"format": "records", "records": [
            {"region": "us", "id": 1, "v": 1},
            {"region": "eu", "id": 9, "v": 1},
            {"region": "us", "id": 1, "v": 2},
        ]},
        config={"target_table": "items", "key_columns": ["region", "id"]}))
    show(res, eng, "items")
    assert res.error["code"] == "SOURCE_DUPLICATE_KEY"

    # ---- 场景 3：目标重复键 -----------------------------------------------
    banner("场景 3：目标重复键 -> STATE_CONFLICT/TARGET_DUPLICATE_KEY")
    conn = store.connect(db)
    store.seed_target(conn, "dirty", ["k1", "k2", "v"], [
        {"k1": "a", "k2": 1, "v": "x"},
        {"k1": "a", "k2": 1, "v": "y"},
    ])
    conn.commit(); conn.close()
    res = eng.run(MergeRequest(
        source={"format": "records", "records": [{"k1": "a", "k2": 1, "v": "z"}]},
        config={"target_table": "dirty", "key_columns": ["k1", "k2"]}))
    show(res, eng, "dirty")
    assert res.error["code"] == "TARGET_DUPLICATE_KEY"

    # ---- 场景 4：提交故障注入 -> 无部分更新 -------------------------------
    banner("场景 4：注入 after_actions 提交失败 -> COMPUTATION_FAILURE + 回滚")
    conn = store.connect(db)
    store.seed_target(conn, "atom", ["k1", "k2", "v"], [
        {"k1": "u", "k2": 1, "v": "original"},
        {"k1": "d", "k2": 2, "v": "doomed"},
    ])
    conn.commit(); conn.close()
    before = eng.get_target_rows("atom")
    res = eng.run(MergeRequest(
        source={"format": "records", "records": [
            {"k1": "u", "k2": 1, "v": "updated"},
            {"k1": "i", "k2": 3, "v": "inserted"},
        ]},
        config={
            "target_table": "atom", "key_columns": ["k1", "k2"],
            "delete_unmatched": True,
            "delete_when": {"op": "eq",
                            "left": {"side": "target", "column": "v"},
                            "right": {"literal": "doomed"}},
        },
        fault_point="after_actions"))
    show(res, eng, "atom")
    after = eng.get_target_rows("atom")
    assert after == before, "PARTIAL UPDATE!"
    print("rollback check: target identical to pre-image  ✓")
    print("(replay: inspect the journal JSONL above, then rerun without fault_point)")

    print("\n全部场景演示完成。临时目录保留在：", root)


if __name__ == "__main__":
    main()
