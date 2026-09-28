"""算法层：长公共前缀 + 压缩结构。

期望值手写；失败即具体类别（节点数不对/顺序不对/上界错误），不是“能调用”。
"""
from __future__ import annotations

from app.normalizer import normalize


def _ids(result):
    return [e.id for e in result]


def test_long_common_prefix_is_compressed(trie_with_corpus, log):
    trie, corpus = trie_with_corpus
    multi_words = [r for r in corpus if normalize(r["surface"]).startswith("multi")]
    log("GIVEN", "GIVEN", multi_words=len(multi_words), nodes=trie.count_nodes())

    # 压缩 Trie 的节点数必须远小于“非压缩” trie 的字符节点数。
    char_nodes = 1 + sum(len(normalize(r["surface"])) for r in corpus)
    log("THEN", "GIVEN", uncompressed_char_nodes=char_nodes, compressed_nodes=trie.count_nodes())
    assert trie.count_nodes() < char_nodes // 2, (
        f"压缩不充分：{trie.count_nodes()} 节点 vs 非压缩 {char_nodes}"
    )
    violations = trie.verify_integrity()
    assert violations == [], f"结构/上界不变量被破坏: {violations[:3]}"
    log("PASS", "PASS", compressed_nodes=trie.count_nodes(), char_nodes=char_nodes)


def test_prefix_multi_returns_exact_order(trie_with_corpus, log):
    trie, _ = trie_with_corpus
    result, trace = trie.top_k("multi", 5, collect_trace=True)
    expected = ["mp-01", "mp-02", "mp-03", "mp-04", "mp-05"]
    got = _ids(result)
    log(
        "THEN",
        "GIVEN",
        prefix="multi",
        k=5,
        expected=expected,
        actual=got,
        visited=trace.stats.nodes_visited,
        pruned=trace.stats.subtrees_pruned,
    )
    assert got == expected, f"top-5 顺序错误: {got}，预期 {expected}"
    # 只访问了前缀子树，不可能触及 db/database 等其他分支。
    assert trace.stats.nodes_visited < trace.stats.total_nodes, "查询遍历了整棵 trie，剪枝未生效"
    log("PASS", "PASS", visited=trace.stats.nodes_visited, total=trace.stats.total_nodes,
        pruned=trace.stats.subtrees_pruned)


def test_query_does_not_walk_full_dictionary(trie_with_corpus, log):
    trie, corpus = trie_with_corpus
    # 一个极短的高选择性 k：只需定位 multit 子树；其他兄弟子树应整体被剪掉。
    result, trace = trie.top_k("multit", 2, collect_trace=True)
    assert _ids(result) == ["mp-05", "mp-06"]
    # 剪枝依据记录完整，每一条都给出上界与第 k 名分数。
    for pfx, reason in trace.prunes:
        assert reason.best_k_score >= 0
        assert reason.upper_bound < reason.best_k_score, "剪枝条件必须是严格小于"
        assert pfx.startswith("multi"), f"被剪子树必须位于前缀子树内: {pfx}"
    log(
        "PASS",
        "PASS",
        prefix="multit",
        pruned_subtrees=len(trace.prunes),
        total_nodes=trace.stats.total_nodes,
        nodes_visited=trace.stats.nodes_visited,
        prune_evidence=[
            {"subtree": p, "upper_bound": r.upper_bound, "best_k": r.best_k_score}
            for p, r in trace.prunes
        ],
    )
    assert trace.stats.subtrees_pruned >= 1, "该场景必须发生子树剪枝"


def test_missing_prefix_is_empty_not_walked(trie_with_corpus, log):
    trie, _ = trie_with_corpus
    result, trace = trie.top_k(normalize("zzz-no-such-prefix"), 5, collect_trace=True)
    assert result == []
    assert trace.location_kind == "missing"
    assert trace.stats.nodes_visited == 0, "missing 前缀不应展开任何节点"
    log("PASS", "PASS", location=trace.location_kind, visited=0)


def test_inside_edge_prefix_location(trie_with_corpus, log):
    """前缀落在压缩边标签内部：'multipr' 处于 multi -> ... 的某条边中间。"""
    trie, _ = trie_with_corpus
    loc = trie.locate_prefix("multipr")
    assert loc.kind in ("inside_edge", "at_node")
    result, _ = trie.top_k("multipr", 5)
    assert _ids(result) == ["mp-01", "mp-02", "mp-03"]
    log("PASS", "PASS", location=loc.kind, located_node_key=loc.node_key)


def test_empty_prefix_returns_global_top_k(trie_with_corpus, log):
    trie, _ = trie_with_corpus
    result, trace = trie.top_k("", 5, collect_trace=True)
    expected = ["ot-3", "mp-01", "ot-1", "mp-02", "mp-03"]
    got = _ids(result)
    assert got == expected, f"空前缀全局 top-5 错误: {got}，预期 {expected}"
    log("PASS", "PASS", prefix="", expected=expected, actual=got,
        visited=trace.stats.nodes_visited, total=trace.stats.total_nodes)
