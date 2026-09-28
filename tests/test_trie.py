"""压缩 Trie 算法测试：具体结果断言 + 失败类别 + 剪枝依据检查。"""

from __future__ import annotations

import pytest

from app.trie import PruneRecord, RadixTrie, TopKTrace
from tests.reference import ReferenceStore


def fill(trie: RadixTrie, specs: list[tuple[str, str, float]]) -> None:
    """按 (id, display, score) 填充；规范化在调用前由参考实现完成。"""
    from tests.reference import ref_normalize

    for eid, display, score in specs:
        trie.upsert(eid, ref_normalize(display), display, float(score))


def ids_of(rows) -> list[str]:
    return [r[2] for r in rows]


LONG_PREFIX_WORDS = [
    ("p1", "internationalization", 10),
    ("p2", "internationalise", 9),
    ("p3", "internationally", 8),
    ("p4", "internet", 20),
    ("p5", "internal", 15),
    ("p6", "interstellar", 5),
    ("p7", "interval", 7),
]


class TestLongCommonPrefix:
    def test_radix_compression_actually_compresses(self):
        t = RadixTrie()
        fill(t, LONG_PREFIX_WORDS)
        # 7 个词共享长前缀，压缩 Trie 节点数必须远小于朴素 trie。
        # 朴素 trie 节点数上界约为所有长度之和+1；这里断言一个紧上界：
        # 压缩后节点数 <= 词条数 * 2 + 1，且实际节点数很小。
        nodes = list(t.walk_nodes())
        total_chars = sum(len(w[1]) for w in LONG_PREFIX_WORDS)
        assert len(nodes) <= 2 * len(LONG_PREFIX_WORDS) + 1
        assert len(nodes) < total_chars, "失败类别: Radix 未发生压缩"
        # 不变量必须健康
        assert t.check_invariants() == []

    def test_prefix_locate_and_results(self, log, run_id):
        t = RadixTrie()
        fill(t, LONG_PREFIX_WORDS)
        ref = ReferenceStore()
        for eid, display, score in LONG_PREFIX_WORDS:
            ref.upsert(eid, display, score)

        for pfx in ["", "inter", "intern", "interna", "internation", "inte", "xyz"]:
            got = ids_of(t.top_k(pfx, 5))
            want = ids_of(ref.ref_top_k(pfx, 5))
            log.info("[%s] 长前缀查询 prefix=%r got=%s want=%s", run_id, pfx, got, want)
            assert got == want, f"失败类别: 长前缀 top-k 不符 prefix={pfx!r}"

    def test_partial_edge_prefix_matches_subtree(self):
        # 前缀落在某条压缩边的中间：整个边子树都属于结果
        t = RadixTrie()
        fill(t, [("a", "internationalization", 3), ("b", "internationalise", 4)])
        # "internatio" 是 "internationalisation..." 边上某段前缀
        rows = ids_of(t.top_k("internatio", 10))
        assert set(rows) == {"a", "b"}

    def test_query_does_not_traverse_whole_dictionary(self, log, run_id):
        # 关键性能语义：高区分前缀只展开极少数节点；用轨迹的
        # terminals_seen 验证没有扫描全部词条。
        t = RadixTrie()
        specs = [(f"w{i}", f"abcdefgh{i:04d}_tail", 1 + (i % 3)) for i in range(200)]
        # 加一个与查询前缀完全无关的大子树
        specs += [(f"z{i}", f"zzzzzzzz{i:04d}_other", 100) for i in range(200)]
        fill(t, specs)

        trace = TopKTrace(prefix_norm="", matched=False, node_id=None)
        t.top_k("abcdefgh0042", 3, trace=trace)
        log.info(
            "[%s] 剪枝统计 pushed=%d expanded=%d terminals_seen=%d pruned=%d pops=%d",
            run_id, trace.pushed, trace.expanded, trace.terminals_seen,
            trace.pruned_children, trace.heap_pops,
        )
        assert trace.matched is True
        # 全词典 400 条；该精确前缀只命中 1 条，绝不可能看见 100+ 终止词条
        assert trace.terminals_seen <= 3, "失败类别: 查询遍历了远超前缀子树的词条"


class TestTieBreaks:
    def test_same_score_stable_canonical_order(self, run_id, log):
        t = RadixTrie()
        words = [
            ("i1", "banana", 5),
            ("i2", "apple", 5),
            ("i3", "cherry", 5),
            ("i4", "avocado", 5),
        ]
        fill(t, words)
        rows = t.top_k("", 4)
        got = [r[1] for r in rows]
        # 同分按 (term_norm, display, id)：apple, avocado, banana, cherry
        want = ["apple", "avocado", "banana", "cherry"]
        log.info("[%s] 同分排序 got=%s want=%s", run_id, got, want)
        assert got == want, "失败类别: 同分稳定排序错误"

    def test_tie_between_colliding_displays_uses_display_then_id(self):
        t = RadixTrie()
        # 三个原文全部规范化到 cafe
        specs = [("z9", "cafe", 7), ("a1", "CAFE", 7), ("m5", "ＣＡＦＥ", 7)]
        fill(t, specs)
        rows = t.top_k("", 10)
        # term_norm 全为 cafe；display 码位序: CAFE(U+0043) < cafe(U+0063) < ＣＡＦＥ(全角)
        got = [(r[1], r[2]) for r in rows]
        assert got == [("CAFE", "a1"), ("cafe", "z9"), ("ＣＡＦＥ", "m5")], \
            f"失败类别: 规范化碰撞同分次序错误: {got}"

    def test_tie_boundary_k_includes_all_equal_scores(self):
        # k=2 但第 2、3 名同分时，算法必须返回完整精确的前 k（按全序决胜），
        # 且结果与全序参考一致，不允许因分数相等而漏掉更强规范键者。
        t = RadixTrie()
        fill(t, [("a", "alpha", 5), ("b", "bravo", 5), ("c", "alpha2", 5),
                 ("d", "delta", 9)])
        rows = ids_of(t.top_k("", 2))
        ref = ReferenceStore()
        for eid, d, s in [("a", "alpha", 5), ("b", "bravo", 5),
                          ("c", "alpha2", 5), ("d", "delta", 9)]:
            ref.upsert(eid, d, s)
        assert rows == ids_of(ref.ref_top_k("", 2)) == ["d", "a"]

    def test_deterministic_across_rebuilds(self):
        specs = [("a", "apple", 1), ("b", "Banana", 2), ("c", "ＡＰＰＬＥ", 2)]
        t1, t2 = RadixTrie(), RadixTrie()
        fill(t1, specs)
        # 逆序插入第二棵树
        fill(t2, list(reversed(specs)))
        assert ids_of(t1.top_k("", 10)) == ids_of(t2.top_k("", 10))


class TestHotwordDemotion:
    def test_lower_score_reorders_and_bound_shrinks(self, run_id, log):
        t = RadixTrie()
        fill(t, [("hot", "prefix_hotword", 100),
                 ("mid", "prefix_middle", 50),
                 ("low", "prefix_low", 10)])
        assert ids_of(t.top_k("prefix", 2)) == ["hot", "mid"]

        # 热词降权到最低（同 id upsert，词频更新路径）
        t.upsert("hot", "prefix_hotword", "prefix_hotword", 1.0)
        rows = ids_of(t.top_k("prefix", 2))
        log.info("[%s] 降权后 top2=%s", run_id, rows)
        assert rows == ["mid", "low"], "失败类别: 热词降权后排序未更新"

        # 上界必须已经沿链收缩：根 max_score 不再是 100
        assert t.root.max_score == 50.0, "失败类别: 降权后根上界仍是旧热词分值"

    def test_demoted_hot_subtree_gets_pruned_by_real_bound(self, run_id, log):
        # 构造一棵“曾经很热”的兄弟子树：降权后其整棵子树上界严格低于阈值，
        # 必须被整枝剪枝，且剪枝记录给出上界依据。
        t = RadixTrie()
        fill(t, [
            ("hot1", "aaa_hot1", 100),
            ("hot2", "aaa_hot2", 99),
            ("cold1", "bbb_cold1", 50),
            ("cold2", "bbb_cold2", 49),
            ("cold3", "bbb_cold3", 48),
        ])
        # 降权 aaa 子树到 1 分
        t.upsert("hot1", "aaa_hot1", "aaa_hot1", 1.0)
        t.upsert("hot2", "aaa_hot2", "aaa_hot2", 0.0)

        trace = TopKTrace(prefix_norm="", matched=False, node_id=None)
        rows = t.top_k("", 2, trace=trace)
        assert ids_of(rows) == ["cold1", "cold2"]
        pruned_labels = [d.edge_label for d in trace.decisions if d.decision == "prune"]
        log.info("[%s] 热词降权剪枝记录: %s", run_id,
                 [(d.edge_label, d.subtree_best_score, d.reason[:40])
                  for d in trace.decisions if d.decision == "prune"])
        # aaa 子树应作为整枝被剪（其上界 1.0 < 阈值 49.0）
        assert any(lab.startswith("aaa") for lab in pruned_labels), \
            "失败类别: 降权后的热子树未被可靠上界整枝剪枝"
        # 每条剪枝记录必须显式记录“子树最优上界 <= 阈值”依据
        for d in trace.decisions:
            if d.decision == "prune" and d.threshold_score is not None:
                assert d.subtree_best_score <= d.threshold_score or d.edge_label == "", \
                    f"失败类别: 剪枝记录缺少有效上界依据: {d}"
        assert t.check_invariants() == []


class TestDeleteAndBound:
    def test_delete_hotword_bound_recomputed(self):
        t = RadixTrie()
        fill(t, [("h", "hot", 100), ("c", "cold", 3)])
        assert t.delete("h", "hot") is True
        assert t.root.max_score == 3.0, "失败类别: 删除热词后上界未重算"
        assert ids_of(t.top_k("", 5)) == ["c"]

    def test_delete_nonexistent_returns_false(self):
        t = RadixTrie()
        fill(t, [("a", "alpha", 1)])
        assert t.delete("ghost", "alpha") is False
        assert t.delete("a", "wrongkey") is False

    def test_delete_prunes_and_compacts(self):
        t = RadixTrie()
        fill(t, [("a", "abcdef", 1), ("b", "abcxyz", 2)])
        assert t.delete("a", "abcdef") is True
        # 删除后剩余树仍可查询且不变量健康（度-1链被压缩）
        assert ids_of(t.top_k("abc", 5)) == ["b"]
        assert t.check_invariants() == []

    def test_delete_then_reinsert_keeps_exact_results(self, run_id, log):
        from tests.reference import ref_normalize

        ref = ReferenceStore()
        t = RadixTrie()
        specs = [("a", "mango", 9), ("b", "maple", 7), ("c", "melon", 7),
                 ("d", "ma", 7)]
        for eid, display, s in specs:
            t.upsert(eid, ref_normalize(display), display, float(s))
            ref.upsert(eid, display, s)

        t.delete("a", "mango"); ref.delete("a")
        t.upsert("a", "mango", "mango", 5.0); ref.upsert("a", "mango", 5)
        for pfx in ["", "ma", "mang"]:
            got = ids_of(t.top_k(pfx, 3))
            want = ids_of(ref.ref_top_k(pfx, 3))
            log.info("[%s] 删改后 pfx=%r got=%s want=%s", run_id, pfx, got, want)
            assert got == want, "失败类别: 删除再插入后结果偏离参考"
        assert t.check_invariants() == []


class TestEmptyPrefixAndBoundHonesty:
    def test_empty_prefix_returns_global_topk(self):
        t = RadixTrie()
        fill(t, [("a", "x1", 1), ("b", "x2", 2), ("c", "y3", 3)])
        assert ids_of(t.top_k("", 2)) == ["c", "b"]

    def test_pruned_subtree_contains_no_better_entry(self, run_id, log):
        """白盒性质：取任意一次剪枝记录，被剪子树里所有词条都必须严格弱于阈值。

        这直接审计“上界可靠、剪枝合法”，而不只是比对最终答案。
        """
        t = RadixTrie()
        rng_specs = []
        for i in range(60):
            rng_specs.append((f"id{i:03d}", f"grp{i % 6}_item{i:03d}", float((i * 7) % 23)))
        fill(t, rng_specs)

        for query_k in [(1, "grp1"), (3, ""), (5, "grp"), (2, "grp3")]:
            k, pfx = query_k
            trace = TopKTrace(prefix_norm="", matched=False, node_id=None)
            rows = t.top_k(pfx, k, trace=trace)
            if len(rows) < k:
                continue  # 阈值未形成时不审计
            threshold_score = rows[-1][3]
            threshold_node = None
            # 找到阈值节点引用：通过重新定位 + 记录中的 node_id 映射
            id_to_node = {n.nid: n for n in t.walk_nodes()}
            for rec in trace.decisions:
                if rec.decision != "prune" or rec.node_id not in id_to_node:
                    continue
                if rec.threshold_score is None:
                    continue
                node = id_to_node[rec.node_id]
                for term, display, eid, score in t.iter_terminals(node):
                    assert score <= rec.threshold_score, (
                        f"失败类别: 非法剪枝 —— 子树 {rec.edge_label!r} 含分值 {score}"
                        f" > 阈值 {rec.threshold_score} 的词条 {eid}"
                    )
            log.info("[%s] pfx=%r k=%d 剪枝审计通过，pruned=%d",
                     run_id, pfx, k, trace.pruned_children)
