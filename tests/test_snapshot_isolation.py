"""快照隔离：匹配只基于操作前目标快照，同批前一行的 INSERT 不可见。

用两类可观测差异验证，而不是依赖实现注释：
  1. 删除集合只包含“操作前未匹配”的目标行——即便后续 INSERT 与某目标键
     发生任何关联，目标集合在决策起点已冻结；
  2. 结构间谍：记录 planner 查询目标索引的顺序与键集合，断言查询发生时
     索引只含操作前行。
"""
from __future__ import annotations

import pytest

from merge_engine import MergeRequest
from merge_engine.contracts import ActionType
from merge_engine import planner as planner_mod
from merge_engine import snapshot as snapshot_mod

from conftest import cfg, seed

A = ActionType

pytestmark = pytest.mark.capture


def test_earlier_insert_cannot_be_matched_by_later_source_row(tmp_path, monkeypatch):
    """白盒结构断言：整轮源决策中 lookup 看到的索引只含操作前行。

    目标只有 (old,0)。源里两个全新的键都会 INSERT。若实现是“边插边匹配”，
    后一行查询索引时应能看到前一行的插入；冻结快照实现下，每次查询的索引
    键集合恒为 {('old',0)}。
    """
    eng = seed(tmp_path, "iso1", ["k1", "k2", "v"], [
        {"k1": "old", "k2": 0, "v": "pre-image"},
    ])

    seen_indexes: list[set] = []
    real_lookup = snapshot_mod.lookup

    def spy(snap, key):
        # 在每次查询时记录当时索引可见的键集合
        seen_indexes.append(set(snap.index.keys()))
        return real_lookup(snap, key)

    monkeypatch.setattr(planner_mod, "lookup", spy)

    req = MergeRequest(
        source={"format": "records", "records": [
            {"k1": "new1", "k2": 1, "v": 1},   # 未匹配 -> INSERT
            {"k1": "new2", "k2": 2, "v": 2},   # 未匹配 -> INSERT
        ]},
        config=cfg("iso1", ("k1", "k2")),
    )
    result = eng.run(req)
    assert result.status == "COMMITTED"

    # 决策期间每次 lookup 看到的索引都只含操作前那一行
    assert seen_indexes, "planner never consulted the target index"
    for idx in seen_indexes:
        assert idx == {("old", 0)}
    # 两次插入都成立
    assert sorted((r["k1"], r["k2"]) for r in eng.get_target_rows("iso1")) == [
        ("new1", 1), ("new2", 2), ("old", 0),
    ]


def test_delete_set_frozen_from_preimage_not_grown_or_shrunk(tmp_path):
    """操作前目标 T=(d,1) status=STALE（应删），源同键 (d,1) 命中匹配则保留。

    另构造目标 K=(d,2) status=ACTIVE：源里没有它的键 -> 未匹配目标，
    但删除条件不成立 -> 保留。关键：源插入 (d,3) 不应改变删除集合的判定
    （朴素“边写边删”实现可能把新插入行也纳入扫描）。
    """
    eng = seed(tmp_path, "iso2", ["k1", "k2", "status"], [
        {"k1": "d", "k2": 1, "status": "STALE"},   # 未匹配 + 条件成立 -> DELETE
        {"k1": "d", "k2": 2, "status": "ACTIVE"},  # 未匹配 + 条件不成立 -> 保留
        {"k1": "m", "k2": 3, "status": "STALE"},   # 会被源匹配 -> 绝不进入删除集合
    ])
    req = MergeRequest(
        source={"format": "records", "records": [
            {"k1": "d", "k2": 3, "status": "NEW"},   # 插入的新行
            {"k1": "m", "k2": 3, "status": "NEW"},   # 匹配旧 STALE 行并更新
        ]},
        config=cfg("iso2", ("k1", "k2"),
                   delete_unmatched=True,
                   delete={"op": "eq",
                           "left": {"side": "target", "column": "status"},
                           "right": {"literal": "STALE"}}),
    )
    result = eng.run(req)
    assert result.status == "COMMITTED"

    writes = [(a.type.value, tuple(a.key)) for a in result.plan.write_actions()]
    # 删除集合恰好是操作前的 (d,1)；匹配行 (m,3) 与新插入 (d,3) 都不删
    deletes = [k for t, k in writes if t == "DELETE_UNMATCHED"]
    assert deletes == [("d", 1)]
    assert ("m", 3) not in deletes and ("d", 3) not in deletes

    rows = {(r["k1"], r["k2"]): r for r in eng.get_target_rows("iso2")}
    assert set(rows) == {("d", 2), ("d", 3), ("m", 3)}
    assert rows[("m", 3)]["status"] == "NEW"


def test_snapshot_fingerprint_stable_and_recorded(tmp_path):
    eng = seed(tmp_path, "iso3", ["k1", "k2", "v"], [
        {"k1": "a", "k2": 1, "v": 1},
    ])
    req = MergeRequest(source={"format": "records", "records": [{"k1": "a", "k2": 1, "v": 2}]},
                       config=cfg("iso3", ("k1", "k2")))
    r1 = eng.run(req)
    fp1 = r1.plan.snapshot_fingerprint
    # 第二次运行的操作前快照是第一次的提交结果；指纹必然不同，且运行元数据记录了它
    r2 = eng.run(req)
    assert r1.plan.snapshot_fingerprint != r2.plan.snapshot_fingerprint
    assert eng.get_run(r1.run_id)["snapshot_fingerprint"] == fp1
    assert eng.get_run(r2.run_id)["snapshot_fingerprint"] == r2.plan.snapshot_fingerprint
