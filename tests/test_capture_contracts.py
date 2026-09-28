"""第三阶段捕获测试（@pytest.mark.capture）。

每个用例的期望动作集合均在注释中手工推导，断言到具体类型/键/理由，
而不是“接口能调用就算过”。
"""
from __future__ import annotations

import pytest

from merge_engine import MergeRequest
from merge_engine.contracts import ActionType

from conftest import actions_of, cfg, seed

A = ActionType

pytestmark = pytest.mark.capture


# ---------------------------------------------------------------------------
# 用例 1：复合键 + 匹配更新 + 未匹配插入 + 条件删除，手工核验动作集合
# ---------------------------------------------------------------------------

def test_composite_key_full_action_set(tmp_path):
    # 目标（复合键 [region, id]）：
    #   (us, 1) 旧值 v=10  -> 源 v=11，无条件 UPDATE
    #   (us, 2) v=20 status=STALE -> 无源行，命中删除条件 -> DELETE
    #   (eu, 9) v=30 status=KEEP  -> 无源行，不满足删除条件 -> 保留，无动作
    # 源：
    #   (us,1) 匹配更新； (us,3) 未匹配，插入条件 v>=5 成立 -> INSERT
    #   (us,4) 未匹配，v=1 不满足插入条件 -> NOOP_UNMATCHED
    eng = seed(tmp_path, "items", ["region", "id", "v", "status"], [
        {"region": "us", "id": 1, "v": 10, "status": "OK"},
        {"region": "us", "id": 2, "v": 20, "status": "STALE"},
        {"region": "eu", "id": 9, "v": 30, "status": "KEEP"},
    ])
    req = MergeRequest(
        source={"format": "records", "records": [
            {"region": "us", "id": 1, "v": 11, "status": "OK"},
            {"region": "us", "id": 3, "v": 5, "status": "OK"},
            {"region": "us", "id": 4, "v": 1, "status": "OK"},
        ]},
        config=cfg(
            "items", ("region", "id"),
            insert={"op": "gte", "left": {"side": "source", "column": "v"},
                    "right": {"literal": 5}},
            delete_unmatched=True,
            delete={"op": "eq", "left": {"side": "target", "column": "status"},
                    "right": {"literal": "STALE"}},
        ),
    )
    result = eng.run(req)
    assert result.status == "COMMITTED"

    # 手工期望（按确定性顺序：源行 rownum 升序，删除按 rowid 升序）
    expected = [
        (A.UPDATE_MATCHED.value, ("us", 1), "MATCHED_UPDATE_COND_TRUE"),
        (A.INSERT_UNMATCHED.value, ("us", 3), "UNMATCHED_INSERT_COND_TRUE"),
        (A.NOOP_UNMATCHED.value, ("us", 4), "UNMATCHED_INSERT_COND_FALSE"),
        (A.DELETE_UNMATCHED.value, ("us", 2), "DELETE_COND_TRUE"),
    ]
    assert actions_of(result) == expected
    assert result.counts == {"UPDATE_MATCHED": 1, "INSERT_UNMATCHED": 1,
                             "DELETE_UNMATCHED": 1}

    # 提交后目标状态逐行核验：更新值生效、插入存在、STALE 行消失、KEEP 行保留
    rows = {(r["region"], r["id"]): r for r in eng.get_target_rows("items")}
    assert set(rows) == {("us", 1), ("us", 3), ("eu", 9)}
    assert rows[("us", 1)]["v"] == 11
    assert rows[("us", 3)]["v"] == 5
    assert rows[("eu", 9)]["status"] == "KEEP"


# ---------------------------------------------------------------------------
# 用例 2：目标重复键（脏目标）-> 状态冲突，动作集合不产生，状态不改变
# ---------------------------------------------------------------------------

def test_target_duplicate_key_is_state_conflict(tmp_path):
    eng = seed(tmp_path, "dup_t", ["k1", "k2", "v"], [
        {"k1": "a", "k2": 1, "v": "first"},
        {"k1": "a", "k2": 1, "v": "second"},   # 与上一行同复合键
        {"k1": "b", "k2": 2, "v": "x"},
    ])
    before = eng.get_target_rows("dup_t")
    req = MergeRequest(
        source={"format": "records", "records": [{"k1": "a", "k2": 1, "v": "new"}]},
        config=cfg("dup_t", ("k1", "k2")),
    )
    result = eng.run(req)
    assert result.status == "REJECTED"
    assert result.error["category"] == "STATE_CONFLICT"
    assert result.error["code"] == "TARGET_DUPLICATE_KEY"
    assert result.error["details"]["duplicates"] == [
        {"key": ["a", 1], "target_rowids": [1, 2], "count": 2}
    ]
    assert result.plan is None
    # 目标状态不变
    assert eng.get_target_rows("dup_t") == before
    meta = eng.get_run(result.run_id)
    assert meta["status"] == "REJECTED"
    assert meta["error"]["code"] == "TARGET_DUPLICATE_KEY"


# ---------------------------------------------------------------------------
# 用例 3：更新影响条件——源值相对目标值的跨表比较
# ---------------------------------------------------------------------------

def test_update_when_compares_source_against_target(tmp_path):
    # score 只有“严格增大”才允许更新；下降/相等 -> NOOP_MATCHED，目标值保留
    eng = seed(tmp_path, "scores", ["team", "round", "score"], [
        {"team": "red", "round": 1, "score": 100},
        {"team": "blue", "round": 1, "score": 100},
    ])
    req = MergeRequest(
        source={"format": "records", "records": [
            {"team": "red", "round": 1, "score": 150},    # 150 > 100 更新
            {"team": "blue", "round": 1, "score": 90},    # 90 < 100 不更新
        ]},
        config=cfg("scores", ("team", "round"), update={
            "op": "gt",
            "left": {"side": "source", "column": "score"},
            "right": {"side": "target", "column": "score"},
        }),
    )
    result = eng.run(req)
    assert actions_of(result) == [
        (A.UPDATE_MATCHED.value, ("red", 1), "MATCHED_UPDATE_COND_TRUE"),
        (A.NOOP_MATCHED.value, ("blue", 1), "MATCHED_UPDATE_COND_FALSE"),
    ]
    rows = {(r["team"], r["round"]): r for r in eng.get_target_rows("scores")}
    assert rows[("red", 1)]["score"] == 150
    assert rows[("blue", 1)]["score"] == 100   # 被条件挡住，旧值保留


def test_update_condition_null_distinguished_from_false(tmp_path):
    # 目标 tag 为 NULL：eq(tag,'GO') 为 NULL（非 TRUE 非 FALSE），
    # reason 必须是 *_NULL 而不是 *_FALSE
    eng = seed(tmp_path, "t3", ["k1", "k2", "tag"], [
        {"k1": "a", "k2": 1, "tag": None},
    ])
    req = MergeRequest(
        source={"format": "records", "records": [{"k1": "a", "k2": 1, "tag": "x"}]},
        config=cfg("t3", ("k1", "k2"), update={
            "op": "eq", "left": {"side": "target", "column": "tag"},
            "right": {"literal": "GO"},
        }),
    )
    result = eng.run(req)
    assert actions_of(result) == [
        (A.NOOP_MATCHED.value, ("a", 1), "MATCHED_UPDATE_COND_NULL")
    ]
    # 条件未成立：目标 tag 仍是 NULL
    rows = eng.get_target_rows("t3")
    assert rows[0]["tag"] is None


# ---------------------------------------------------------------------------
# 用例 4：源内同键多行 -> 一次性拒绝，且与行序无关
# ---------------------------------------------------------------------------

def test_source_duplicate_keys_rejected_all_conflicts_reported(tmp_path):
    eng = seed(tmp_path, "t4", ["k1", "k2", "v"],
               [{"k1": "z", "k2": 0, "v": "sentinel"}])
    records = [
        {"k1": "a", "k2": 1, "v": 1},
        {"k1": "b", "k2": 2, "v": 1},
        {"k1": "a", "k2": 1, "v": 2},   # 与第 1 行冲突
        {"k1": "b", "k2": 2, "v": 2},   # 与第 2 行冲突（间隔出现，非相邻）
        {"k1": "a", "k2": 1, "v": 3},   # 第三次出现
    ]
    req = MergeRequest(source={"format": "records", "records": records},
                       config=cfg("t4", ("k1", "k2")))
    result = eng.run(req)
    assert result.status == "REJECTED"
    assert result.error["category"] == "INPUT_ERROR"
    assert result.error["code"] == "SOURCE_DUPLICATE_KEY"

    dups = {(tuple(d["key"])): d["rownums"]
            for d in result.error["details"]["duplicates"]}
    # 全部冲突都在一次拒绝中给出（不只报第一个）
    assert dups[("a", 1)] == [1, 3, 5]
    assert dups[("b", 2)] == [2, 4]
    # 被拒绝：目标完全不动
    rows = eng.get_target_rows("t4")
    assert len(rows) == 1 and rows[0]["v"] == "sentinel"


def test_source_duplicate_rejection_independent_of_order(tmp_path):
    """打乱源行顺序（让重复键以不同次序出现），拒绝结果必须一致。"""
    eng = seed(tmp_path, "t4b", ["k1", "k2", "v"], [])
    base = [
        {"k1": "a", "k2": 1, "v": 1},
        {"k1": "a", "k2": 1, "v": 2},
        {"k1": "c", "k2": 3, "v": 9},
    ]
    orderings = [base, list(reversed(base)), [base[2], base[0], base[1]]]
    for records in orderings:
        expected_rownums = sorted(
            i for i, r in enumerate(records, start=1)
            if (r["k1"], r["k2"]) == ("a", 1)
        )
        result = eng.run(MergeRequest(
            source={"format": "records", "records": records},
            config=cfg("t4b", ("k1", "k2")),
        ))
        assert result.status == "REJECTED"
        assert result.error["code"] == "SOURCE_DUPLICATE_KEY"
        dup = result.error["details"]["duplicates"]
        assert len(dup) == 1
        assert list(dup[0]["key"]) == ["a", 1]
        # 报告的行号必须与本次输入中的真实位置一致
        assert sorted(dup[0]["rownums"]) == expected_rownums


# ---------------------------------------------------------------------------
# 用例 5：NULL 相等策略
# ---------------------------------------------------------------------------

def test_sql_null_equality_rejects_null_source_key(tmp_path):
    eng = seed(tmp_path, "t5", ["k1", "k2", "v"], [])
    req = MergeRequest(
        source={"format": "records", "records": [
            {"k1": "a", "k2": 1, "v": 1},
            {"k1": None, "k2": 2, "v": 2},   # 键列 NULL
        ]},
        config=cfg("t5", ("k1", "k2"), null="SQL"),
    )
    result = eng.run(req)
    assert result.status == "REJECTED"
    assert result.error["code"] == "KEY_NULL_REJECTED"
    assert result.error["details"]["rows"][0]["rownum"] == 2
    assert result.error["details"]["rows"][0]["key"] == [None, 2]


def test_distinct_null_equality_matches_nulls(tmp_path):
    # DISTINCT 策略：NULL IS NOT DISTINCT FROM NULL -> 同一复合键
    eng = seed(tmp_path, "t5b", ["k1", "k2", "v"], [
        {"k1": None, "k2": 1, "v": "old"},
    ])
    req = MergeRequest(
        source={"format": "records", "records": [
            {"k1": None, "k2": 1, "v": "new"},
        ]},
        config=cfg("t5b", ("k1", "k2"), null="DISTINCT"),
    )
    result = eng.run(req)
    assert result.status == "COMMITTED"
    assert actions_of(result) == [
        (A.UPDATE_MATCHED.value, (None, 1), "MATCHED_UPDATE_COND_TRUE")
    ]
    rows = eng.get_target_rows("t5b")
    assert len(rows) == 1 and rows[0]["v"] == "new"


def test_distinct_null_duplicate_target_conflict(tmp_path):
    # DISTINCT 下两个全 NULL 键目标行互为重复 -> 状态冲突
    eng = seed(tmp_path, "t5c", ["k1", "k2", "v"], [
        {"k1": None, "k2": None, "v": 1},
        {"k1": None, "k2": None, "v": 2},
    ])
    req = MergeRequest(
        source={"format": "records", "records": [{"k1": None, "k2": None, "v": 3}]},
        config=cfg("t5c", ("k1", "k2"), null="DISTINCT"),
    )
    result = eng.run(req)
    assert result.status == "REJECTED"
    assert result.error["code"] == "TARGET_DUPLICATE_KEY"


def test_sql_null_target_rows_never_match_and_never_duplicate(tmp_path):
    # SQL 策略：目标含 NULL 键行时，源不提供该键 -> 该行留待删除集合按条件处理，
    # 且两个 NULL 键目标行不算重复
    eng = seed(tmp_path, "t5d", ["k1", "k2", "v", "status"], [
        {"k1": None, "k2": None, "v": 1, "status": "STALE"},
        {"k1": None, "k2": None, "v": 2, "status": "STALE"},
    ])
    req = MergeRequest(
        source={"format": "records", "records": [
            {"k1": "a", "k2": 1, "v": 9, "status": "OK"},
        ]},
        config=cfg("t5d", ("k1", "k2"), null="SQL",
                   delete_unmatched=True,
                   delete={"op": "eq", "left": {"side": "target", "column": "status"},
                           "right": {"literal": "STALE"}}),
    )
    result = eng.run(req)
    assert result.status == "COMMITTED"
    # 两个 NULL 键目标行进入未匹配删除集合
    deletes = [a for a in actions_of(result) if a[0] == A.DELETE_UNMATCHED.value]
    assert sorted(k for _, k, _ in deletes) == [(None, None), (None, None)]
    rows = eng.get_target_rows("t5d")
    assert [(r["k1"], r["k2"]) for r in rows] == [("a", 1)]
