"""Engine + SQLite 版本存储测试：持久化、版本链、快照恢复、错误语义。"""

from __future__ import annotations

import math

import pytest

from app.engine import Engine
from app.errors import (
    EntryNotFound,
    ValidationFailure,
    VersionNotFound,
)
from app.normalize import NORMALIZER_VERSION
from tests.reference import ReferenceStore


def upsert(engine: Engine, specs: list[tuple[str, str, float]], note: str = "") -> int:
    payload = [{"id": eid, "term": term, "score": score} for eid, term, score in specs]
    version_id, ins, upd, _ = engine.bulk_upsert(payload, note=note)
    return version_id


class TestPersistence:
    def test_reopen_rebuilds_identical_index(self, tmp_path, run_id, log):
        db = tmp_path / "persist.db"
        e1 = Engine(str(db))
        v = upsert(e1, [("a", "Hello", 5), ("b", "ＣＡＦＥ", 9),
                        ("c", "Straße", 3), ("d", "cafe", 9)])
        before = e1.complete("", 10).rows
        log.info("[%s] 关闭前 head=v%s 结果数=%d", run_id, v, len(before))

        # 重新打开：从 SQLite 重建
        e2 = Engine(str(db))
        assert e2.storage.head_version == v
        after = e2.complete("", 10).rows
        assert before == after, "失败类别: 重启后重建索引结果不同"
        # 规范化碰撞在持久化后仍同时存在且同分按 display 决胜
        ids_ = [r[2] for r in after if r[0] == "cafe"]
        assert set(ids_) == {"b", "d"}
        assert e2.check_invariants() == []

    def test_versions_chain_parent_ids(self, engine):
        v1 = upsert(engine, [("a", "apple", 1)], note="first")
        v2 = upsert(engine, [("b", "banana", 2)], note="second")
        v3, _, _, _ = engine.bulk_upsert(
            [{"id": "a", "term": "apple", "score": 100}], note="promote"
        )
        versions = {row["version_id"]: row for row in engine.storage.list_versions()}
        assert versions[1]["kind"] == "baseline"
        assert versions[v1]["parent_id"] == 1
        assert versions[v2]["parent_id"] == v1
        assert versions[v3]["parent_id"] == v2
        # 词频更新后 apple 排第一
        assert engine.complete("", 2).rows[0][2] == "a"

    def test_bulk_upsert_is_atomic(self, engine):
        good = [{"id": "g1", "term": "good", "score": 1}]
        # 第二批包含非法分值（NaN）：整批必须失败且没有任何部分写入
        engine.bulk_upsert(good)
        bad = [
            {"id": "g2", "term": "fine", "score": 2},
            {"id": "g3", "term": "bad", "score": float("nan")},
        ]
        with pytest.raises(ValidationFailure):
            engine.bulk_upsert(bad)
        assert engine.storage.get_entry("g2") is None
        assert engine.storage.get_entry("g3") is None
        assert engine.storage.count_entries() == 1
        # 失败不污染索引
        assert engine.check_invariants() == []

    def test_invalid_scores_and_terms(self, engine):
        for bad_score in (float("inf"), float("-inf")):
            with pytest.raises(ValidationFailure):
                engine.bulk_upsert([{"id": "x", "term": "t", "score": bad_score}])
        with pytest.raises(ValidationFailure):
            engine.bulk_upsert([{"id": "x", "term": "t", "score": "not-a-number"}])
        with pytest.raises(ValidationFailure):
            engine.complete("a", k=0)
        with pytest.raises(ValidationFailure):
            engine.complete("a", k=10_000)
        assert engine.check_invariants() == []

    def test_delete_requires_existing_entry(self, engine):
        upsert(engine, [("a", "apple", 1)])
        with pytest.raises(EntryNotFound):
            engine.delete_entry("ghost")
        v, deleted = engine.delete_entry("a")
        assert deleted is True and v >= 2
        assert engine.complete("", 10).rows == []

    def test_crosscheck_with_reference_after_mixed_ops(self, run_id, log):
        import random

        rng = random.Random(99)
        engine = Engine(f"/tmp/ctrie-cross-{run_id}.db")
        ref = ReferenceStore()
        ids: list[str] = []
        serial = 0
        for step in range(120):
            words = ["apple", "application", "APPLY", "ＣＡＦＥ", "cafe",
                     "Straße", "street", "ﬁle", "film", "①st"]
            r = rng.random()
            if r < 0.6 or not ids:
                serial += 1
                eid = f"id{serial}"
                w = rng.choice(words)
                s = rng.randint(0, 10)
                upsert(engine, [(eid, w, s)])
                ref.upsert(eid, w, s)
                ids.append(eid)
            elif r < 0.8:
                # 词频更新/改名：必须复用已有 id（engine 以 id 定位词条）
                eid = rng.choice(ids)
                w = rng.choice(words)
                s = rng.randint(0, 10)
                upsert(engine, [(eid, w, s)])
                ref.upsert(eid, w, s)
            else:
                idx = rng.randrange(len(ids))
                eid = ids.pop(idx)
                engine.delete_entry(eid)
                ref.delete(eid)
            assert engine.check_invariants() == []
            pfx = rng.choice(["", "ap", "cafe", "str", "ﬁ", "1", "zz"])
            got = [(r[0], r[1], r[2], r[3]) for r in engine.complete(pfx, 4).rows]
            want = ref.ref_top_k(pfx, 4)
            assert got == want, f"失败类别: Engine 混合操作交叉不一致 step={step} pfx={pfx!r}"
        log.info("[%s] Engine 混合操作 120 步交叉验证通过", run_id)


class TestSnapshots:
    def test_snapshot_and_historical_query(self, engine, run_id, log):
        v1 = upsert(engine, [("a", "apple", 10), ("b", "banana", 5)])
        snap = engine.snapshot("s1")
        snap_v = snap["version"]
        # 之后大幅修改
        upsert(engine, [("a", "apple", 1), ("c", "cherry", 99)])
        engine.delete_entry("b")

        current = [r[2] for r in engine.complete("", 5).rows]
        historical = [r[2] for r in engine.complete("", 5, version=snap_v).rows]
        baseline = engine.complete("", 5, version=1).rows
        log.info("[%s] 当前=%s 快照v%s=%s baseline=%s",
                 run_id, current, snap_v, historical, baseline)
        assert current == ["c", "a"], "失败类别: 当前视图错误"
        assert historical == ["a", "b"], "失败类别: 历史快照查询错误"
        assert baseline == [], "失败类别: baseline 应为空"

        # 普通 commit 版本不可作为历史查询目标（未物化）
        with pytest.raises(VersionNotFound):
            engine.complete("", 5, version=v1)

    def test_restore_snapshot(self, engine):
        upsert(engine, [("a", "apple", 10), ("b", "banana", 5)])
        snap_v = engine.snapshot("freeze")["version"]
        upsert(engine, [("z", "zoo", 100)])
        assert engine.complete("", 1).rows[0][2] == "z"

        info = engine.restore(snap_v)
        assert set(r[2] for r in engine.complete("", 10).rows) == {"a", "b"}
        assert info["source_snapshot_version"] == snap_v
        # 恢复产生新版本且不变量健康
        assert engine.storage.head_version == info["version"]
        assert engine.check_invariants() == []

    def test_restore_nonexistent_snapshot(self, engine):
        with pytest.raises(VersionNotFound):
            engine.restore(999)

    def test_snapshot_persists_across_restart(self, tmp_path):
        db = tmp_path / "snap.db"
        e = Engine(str(db))
        upsert(e, [("a", "apple", 10)])
        snap_v = e.snapshot("persisted")["version"]
        upsert(e, [("b", "banana", 1)])
        e2 = Engine(str(db))
        rows = [r[2] for r in e2.complete("", 10, version=snap_v).rows]
        assert rows == ["a"]
