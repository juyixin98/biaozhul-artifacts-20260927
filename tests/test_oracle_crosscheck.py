"""独立 oracle 交叉核对（成功路径）+ 行序不变性 + 全部三种输入格式。

参考答案来自 tests/oracle.py（独立命令式实现），不调用被测内核。
"""
from __future__ import annotations

import itertools
import json
import random

import pytest

from merge_engine import MergeRequest

from conftest import cfg, seed
from oracle import OAction, oracle_plan

pytestmark = pytest.mark.capture


KEY_DOMAIN = [("a", 1), ("a", 2), ("b", 1), ("b", 2), ("c", 9)]
# 仅使用非 NULL 键值做成功路径（NULL 策略在专门用例覆盖）
VALUES = [1, 5, 9, 100, None, "x"]
STATUSES = ["STALE", "ACTIVE", None]


def _gen_case(seed_: int):
    rng = random.Random(seed_)
    # 源：从键域取一个无重复子集
    n_src = rng.randint(0, len(KEY_DOMAIN))
    src_keys = rng.sample(KEY_DOMAIN, n_src)
    source = [
        {"k1": k1, "k2": k2, "v": rng.choice(VALUES), "status": rng.choice(STATUSES)}
        for k1, k2 in src_keys
    ]
    # 目标：另取一个无重复子集（保证成功路径无目标重复）
    n_tgt = rng.randint(0, len(KEY_DOMAIN))
    tgt_keys = rng.sample(KEY_DOMAIN, n_tgt)
    target = [
        {"k1": k1, "k2": k2, "v": rng.choice(VALUES), "status": rng.choice(STATUSES)}
        for k1, k2 in tgt_keys
    ]
    config = cfg(
        "prop", ("k1", "k2"),
        update={"op": "gt",
                "left": {"side": "source", "column": "v"},
                "right": {"side": "target", "column": "v"}},
        insert={"op": "gte",
                "left": {"side": "source", "column": "v"},
                "right": {"literal": 5}},
        delete_unmatched=True,
        delete={"op": "eq",
                "left": {"side": "target", "column": "status"},
                "right": {"literal": "STALE"}},
    )
    return source, target, config


@pytest.mark.parametrize("seed_", range(40))
def test_engine_matches_independent_oracle(tmp_path, seed_):
    source, target, config = _gen_case(seed_)
    target_rows = [(i + 1, row) for i, row in enumerate(target)]

    # 1) oracle 先给出独立答案
    err, expected_actions = oracle_plan(source, target_rows, config)
    assert err is None, f"case generator produced error case at seed {seed_}: {err}"

    # 2) 被测内核
    eng = seed(tmp_path, "prop", ["k1", "k2", "v", "status"], target)
    result = eng.run(MergeRequest(
        source={"format": "records", "records": source}, config=config))
    assert result.status == "COMMITTED", result.error
    actual = [
        OAction(a.type.value, tuple(a.key), a.source_rownum, a.target_rowid, a.reason)
        for a in result.plan.actions
    ]
    assert [(a.type, a.key, a.source_rownum, a.target_rowid, a.reason)
            for a in actual] == [
        (a.type, a.key, a.source_rownum, a.target_rowid, a.reason)
        for a in expected_actions
    ]


def test_shuffle_invariance_with_oracle(tmp_path):
    """源行全排列下，键->动作类型的映射不变（行号随排列变化，按键归一）。

    每个排列用一套全新库 + 相同操作前快照，避免前一次提交污染后一次判定。
    """
    source, target, config = _gen_case(20260927)
    if len(source) < 2:
        source, target, config = _gen_case(7)

    def by_key(idx, records):
        eng = _fresh_engine(tmp_path, f"sh{idx}",
                            ("prop_sh", ["k1", "k2", "v", "status"], target))
        result = eng.run(MergeRequest(
            source={"format": "records", "records": records}, config=config))
        assert result.status == "COMMITTED", result.error
        return {tuple(a.key): (a.type.value, a.reason) for a in result.plan.actions}

    perms = list(itertools.islice(itertools.permutations(source), 0, 6))
    baselines = [by_key(i, list(p)) for i, p in enumerate(perms)]
    for b in baselines[1:]:
        assert b == baselines[0]


def _fresh_engine(tmp_path, name, table_target):
    """在独立子目录里建一套库并灌入相同目标快照。"""
    from merge_engine import MergeEngine
    from merge_engine import store as st
    sub = tmp_path / name
    sub.mkdir(exist_ok=True)
    db = sub / "m.db"
    eng = MergeEngine(db, sub / "journal")
    conn = st.connect(db)
    try:
        st.ensure_meta(conn)
        st.seed_target(conn, table_target[0], table_target[1], table_target[2])
        conn.commit()
    finally:
        conn.close()
    return eng


def test_ndjson_and_parquet_inputs_agree_with_records(tmp_path, make_parquet):
    source, target, config = _gen_case(99)
    insert_cond = {"op": "gte", "left": {"side": "source", "column": "v"},
                   "right": {"literal": 5}}

    eng1 = seed(tmp_path, "p1", ["k1", "k2", "v", "status"], target)
    r1 = eng1.run(MergeRequest(
        source={"format": "records", "records": source},
        config=cfg("p1", ("k1", "k2"), insert=insert_cond)))
    sig1 = [(a.type.value, tuple(a.key), a.reason) for a in r1.plan.actions]

    # NDJSON
    eng2 = _fresh_engine(tmp_path, "nd", ("p2", ["k1", "k2", "v", "status"], target))
    content = "\n".join(json.dumps(r) for r in source)
    r2 = eng2.run(MergeRequest(
        source={"format": "ndjson", "content": content},
        config=cfg("p2", ("k1", "k2"), insert=insert_cond)))
    sig2 = [(a.type.value, tuple(a.key), a.reason) for a in r2.plan.actions]
    assert sig2 == sig1

    # Parquet
    p = make_parquet("src.parquet", source)
    eng3 = _fresh_engine(tmp_path, "pq", ("p3", ["k1", "k2", "v", "status"], target))
    r3 = eng3.run(MergeRequest(
        source={"format": "parquet", "path": str(p)},
        config=cfg("p3", ("k1", "k2"), insert=insert_cond)))
    sig3 = [(a.type.value, tuple(a.key), a.reason) for a in r3.plan.actions]
    assert sig3 == sig1
