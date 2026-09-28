"""效率保证：查询不得遍历全词典再排序。

生成本地合成大词典，断言：
- trie 查询访问的节点数远小于词典规模，且随词典增大基本由前缀子树决定；
- 结果与独立全量 oracle 一致；
- 剪枝只在依据成立时发生（每次剪枝的上界被独立核算）。
"""
from __future__ import annotations

import time

from app.normalizer import normalize
from app.oracle import oracle_subtree_max, oracle_top_k
from app.trie import CompressedTrie, Entry


def _synthetic_entries(size: int, seed: int = 20260928) -> list[Entry]:
    # 确定性合成：大量长公共前缀词 + 少量其他分支词。
    import random

    rng = random.Random(seed)
    long_roots = ["multiprocessor-subsystem-", "infrastructure-monitoring-", "telemetry-pipeline-"]
    entries: list[Entry] = []
    for i in range(size):
        root = long_roots[i % len(long_roots)]
        surface = f"{root}{i:06d}"
        entries.append(Entry(f"s{i}", surface, normalize(surface), rng.randint(0, 100_000)))
    # 加入一批完全不同前缀的高频噪声词。
    for j in range(size // 5):
        surface = f"zz-noise-{j:06d}"
        entries.append(Entry(f"n{j}", surface, normalize(surface), rng.randint(0, 100_000)))
    return entries


def test_selective_query_does_not_scan_all_entries():
    for size in (500, 2000):
        entries = _synthetic_entries(size)
        trie = CompressedTrie()
        for e in entries:
            trie.upsert(e)
        assert trie.verify_integrity() == []

        # 高选择性前缀：只命中某一长根下的词。
        pfx = normalize("multiprocessor-subsystem-0000")
        t0 = time.perf_counter()
        got, trace = trie.top_k(pfx, 10, collect_trace=True)
        elapsed = time.perf_counter() - t0

        exp = [e.id for e in oracle_top_k(entries, pfx, 10).entries]
        assert [e.id for e in got] == exp, "大词典下结果与 oracle 不一致"

        # 关键效率断言：看到的词条数远小于词典总数（不是全量筛选再排序）。
        assert trace.stats.entries_seen <= max(50, len(entries) // 50), (
            f"size={size} 查询看到 {trace.stats.entries_seen} 词条，疑似全量扫描"
        )
        assert trace.stats.nodes_visited < trace.stats.total_nodes // 10
        assert elapsed < 1.0, f"查询耗时 {elapsed:.3f}s 异常"

        # 上界依据：被剪掉的子树（噪声分支）上界必须等于暴力真实最大值。
        for sub_pfx, reason in trace.prunes:
            true_max = oracle_subtree_max(entries, sub_pfx)
            assert true_max is not None
            assert reason.upper_bound == true_max, (sub_pfx, reason.upper_bound, true_max)


def test_empty_prefix_topk_still_bounded():
    entries = _synthetic_entries(1000)
    trie = CompressedTrie()
    for e in entries:
        trie.upsert(e)
    got, trace = trie.top_k("", 5, collect_trace=True)
    exp = [e.id for e in oracle_top_k(entries, "", 5).entries]
    assert [e.id for e in got] == exp
    # 即使空前缀，best-first + 剪枝也不应把每个 term 都塞进堆比较：
    # entries_seen 受上界保护后显著小于全量。
    assert trace.stats.entries_seen < len(entries)
    for sub_pfx, reason in trace.prunes:
        assert reason.upper_bound < reason.best_k_score
