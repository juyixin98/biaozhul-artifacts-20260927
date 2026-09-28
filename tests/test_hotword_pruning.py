"""热词降权/删除后的上界正确性——防止错误剪枝的核心场景。

策略：
1. 先让高分词条占据 top-k，再把它降权/删除；
2. 断言降权后新 top-k 与独立暴力答案一致（证明没有因过期高上界而漏候选）；
3. 对每次剪枝记录的上界，用暴力方式独立核算子树真实最大词频，必须相等；
4. 随机模型下对拍数千个前缀。
"""
from __future__ import annotations

import random

import pytest

from app.normalizer import normalize
from app.oracle import oracle_subtree_max, oracle_top_k
from app.errors import IndexCorrupt
from app.trie import CompressedTrie, Entry


def _build(trie, corpus):
    for r in corpus:
        trie.upsert(Entry(r["id"], r["surface"], normalize(r["surface"]), r["score"]))


def test_hotword_downweight_does_not_overprune(trie_with_corpus, log):
    trie, corpus = trie_with_corpus
    # 降权前 mp-01(95) 稳居 multi top-1。
    before = [e.id for e in trie.top_k("multi", 3)[0]]
    assert before == ["mp-01", "mp-02", "mp-03"]
    log("GIVEN", "GIVEN", top3_before=before)

    # 把热词降到 1 分。
    trie.upsert(Entry("mp-01", "multiprocessing", "multiprocessing", 1))
    assert trie.verify_integrity() == [], "降权后上界未同步重算"
    after = [e.id for e in trie.top_k("multi", 3)[0]]
    expected = ["mp-02", "mp-03", "mp-04"]
    log("THEN", "GIVEN", top3_after=after, expected=expected, action="mp-01 score 95->1")
    assert after == expected, (
        f"热词降权后结果错误（典型症状：过期上界导致错误剪枝）: {after}，预期 {expected}"
    )

    # 独立 oracle 对拍。
    entries = []
    for r in corpus:
        score = 1 if r["id"] == "mp-01" else r["score"]
        entries.append(Entry(r["id"], r["surface"], normalize(r["surface"]), score))
    exp = [e.id for e in oracle_top_k(entries, "multi", 3).entries]
    assert after == exp, f"与暴力答案不一致: {after} vs {exp}"
    log("PASS", "PASS", after=after, oracle=exp)


def test_hotword_delete_does_not_overprune(trie_with_corpus, log):
    trie, corpus = trie_with_corpus
    # 删除全部 80/95 分的 multi* 词后，70 分词必须顶上来。
    for eid in ("mp-01", "mp-02", "mp-03"):
        assert trie.delete(eid) is True
    assert trie.verify_integrity() == [], "删除后结构/上界损坏"
    after, trace = trie.top_k("multi", 3, collect_trace=True)
    got = [e.id for e in after]
    expected = ["mp-04", "mp-05", "mp-06"]
    log("THEN", "GIVEN", after_delete=got, expected=expected)
    assert got == expected, f"删除热词后补位错误: {got}，预期 {expected}（可能被过期上界剪掉）"
    # 删除不存在的 id 必须是明确失败而非静默成功。
    assert trie.delete("mp-01") is False
    log("PASS", "PASS", after=got, nodes=trie.count_nodes())


def test_promote_dark_horse_after_downweight(trie_with_corpus, log):
    """低分冷门词升权后必须能被检索到（上界是上界，上升也要及时传播）。"""
    trie, _ = trie_with_corpus
    trie.upsert(Entry("mp-08", "multicast", "multicast", 999))
    top1, trace = trie.top_k("multi", 1, collect_trace=True)
    assert top1[0].id == "mp-08"
    assert trie.verify_integrity() == []
    # 根上界也要更新：空前缀 top-1 应是它。
    global_top, _ = trie.top_k("", 1)
    assert global_top[0].id == "mp-08" and global_top[0].score == 999
    log("PASS", "PASS", promoted="mp-08", root_max=global_top[0].score)


def test_every_prune_bound_matches_bruteforce_subtree_max(trie_with_corpus, log):
    """逐条核对剪枝上界：trie 声称的上界 == 暴力子树最大词频。"""
    trie, corpus = trie_with_corpus
    entries = [Entry(r["id"], r["surface"], normalize(r["surface"]), r["score"]) for r in corpus]
    checked = 0
    for pfx in ["", "m", "multi", "multip", "multic", "multit", "d", "da"]:
        _, tr = trie.top_k(normalize(pfx), 3, collect_trace=True)
        for sub_pfx, reason in tr.prunes:
            true_max = oracle_subtree_max(entries, sub_pfx)
            assert true_max is not None, f"被剪子树 {sub_pfx!r} 竟然没有候选"
            assert reason.upper_bound == true_max, (
                f"子树 {sub_pfx!r} 上界 {reason.upper_bound} != 真实最大值 {true_max}，"
                "上界不可靠会导致漏结果"
            )
            assert reason.upper_bound < reason.best_k_score, "剪枝必须严格小于第 k 名"
            checked += 1
    log("PASS", "PASS", prune_bounds_checked=checked)
    assert checked > 0, "夹具应至少触发一次剪枝，否则本测试没有检验力"


def test_equal_scores_are_never_pruned(trie_with_corpus, log):
    """上界 == 第 k 名分数时不得剪枝（同分候选可能凭稳定键进入结果）。"""
    trie, _ = trie_with_corpus
    _, tr = trie.top_k("multicast", 2, collect_trace=True)
    # 4 个同分 40 的碰撞词取前 2，任何包含剩余碰撞词的子树都不能因 40==40 被剪掉。
    got = [e.id for e in trie.top_k("multicast", 2)[0]]
    assert got == ["col-2", "col-3"]
    for _sub, reason in tr.prunes:
        assert reason.upper_bound != reason.best_k_score or reason.upper_bound < reason.best_k_score
    log("PASS", "PASS", top2=got, rule="equal bound is never pruned")


def test_stale_bound_is_detected_as_corruption(trie_with_corpus, log):
    """模拟“热词降权后上界未重算”的陈旧状态：诊断必须报 E_INDEX_CORRUPT。"""
    trie, _ = trie_with_corpus
    # 找到根节点记录的子树最大值对应的真实来源；直接把某内部节点上界改高，
    # 构造不可靠上界。verify 必须独立重算并发现漂移，而不是信任缓存。
    root = trie.root
    original = root.subtree_max
    root.subtree_max = (original or 0) + 10_000
    violations = trie.verify_integrity()
    assert any(v["type"] == "stale_subtree_max" for v in violations), violations
    log("PASS", "PASS", detected="stale_subtree_max",
        claimed=root.subtree_max, violations=len(violations))

    # engine.verify 必须把它升级成 IndexCorrupt，而不是 ok=true。
    from app.config import Settings
    from app.engine import Engine
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        settings = Settings.from_env(
            {"TRIE_DATA_DIR": str(Path(d) / "data"), "TRIE_LOG_DIR": str(Path(d) / "logs")}
        )
        eng = Engine(settings.db_path, settings.snapshot_dir)
        eng.upsert("x1", "multiprocessing", 50)
        eng.trie.root.subtree_max = 999_999
        with pytest.raises(IndexCorrupt):
            eng.verify()
        log("PASS", "PASS", raised="E_INDEX_CORRUPT")
        eng.close()


@pytest.mark.parametrize("seed", [1, 7, 42, 123, 999])
def test_randomized_oracle_differential(seed, log):
    """随机增删改后对拍独立 oracle；参考答案不来自被测实现。"""
    rng = random.Random(seed)
    stems = ["ab", "abc", "abcd", "abx", "abxy", "ac", "ace", "z", "zoo", "ＡＢ", "éclair"]
    surfaces = sorted({
        "".join(rng.choice(stems) for _ in range(rng.randint(1, 3))) for _ in range(120)
    })
    surfaces = sorted({s for s in surfaces if normalize(s)})
    entries = {
        f"w{i}": Entry(f"w{i}", s, normalize(s), rng.randint(0, 50))
        for i, s in enumerate(surfaces)
    }
    trie = CompressedTrie()
    for e in entries.values():
        trie.upsert(e)

    def check(round_no):
        all_e = list(entries.values())
        for pfx in [""] + sorted({e.key[:n] for e in all_e for n in (1, 2)}):
            for k in (1, 3, 5):
                got = [e.id for e in trie.top_k(pfx, k)[0]]
                exp = [e.id for e in oracle_top_k(all_e, pfx, k).entries]
                assert got == exp, (
                    f"seed={seed} round={round_no} pfx={pfx!r} k={k}: trie={got} oracle={exp}"
                )

    check(0)
    ids = list(entries)
    for round_no in range(1, 121):
        eid = rng.choice(ids)
        move = rng.random()
        if move < 0.35:
            e = entries[eid]
            entries[eid] = Entry(eid, e.surface, e.key, rng.randint(0, 50))
        elif move < 0.55:
            s = rng.choice(surfaces)
            entries[eid] = Entry(eid, s, normalize(s), rng.randint(0, 50))
        else:
            trie.delete(eid)
            del entries[eid]
            ids.remove(eid)
            if not ids:
                break
            continue
        trie.upsert(entries[eid])
        if round_no % 20 == 0:
            assert trie.verify_integrity() == []
            check(round_no)
    assert trie.verify_integrity() == []
    check(999)
    log("PASS", "PASS", seed=seed, remaining=len(entries))
