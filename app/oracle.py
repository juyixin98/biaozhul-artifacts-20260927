"""独立暴力参照实现（测试 oracle）。

刻意与 ``trie.py`` 没有任何实现复用：它直接对词条集合做全量前缀筛选再排序，
用 Python 内置排序。测试以它为“标准答案”对照压缩 Trie 的分支限界结果，
因此答案不是由被测核心自身生成的。

小夹具的“具体期望值”在测试里手写（不调用本模块），双重防止自证。
"""
from __future__ import annotations

from dataclasses import dataclass

from .normalizer import normalize
from .trie import Entry, entry_sort_tuple


@dataclass(frozen=True, slots=True)
class OracleResult:
    entries: list[Entry]
    candidates_scanned: int


def oracle_top_k(
    entries: list[Entry],
    raw_prefix: str,
    k: int,
) -> OracleResult:
    """全量扫描：规范化前缀 -> startswith 筛选 -> 稳定排序取前 k。

    时间复杂度 O(N log N)，与 trie 的剪枝查询形成“正确性基线 vs 高效实现”对照。
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    pfx = normalize(raw_prefix)
    candidates = [e for e in entries if e.key.startswith(pfx)]
    candidates.sort(key=entry_sort_tuple)
    return OracleResult(entries=candidates[:k], candidates_scanned=len(candidates))


def oracle_subtree_max(entries: list[Entry], normalized_prefix: str) -> int | None:
    """暴力计算某前缀子树的最大词频——独立版“可靠上界”。"""
    scores = [e.score for e in entries if e.key.startswith(normalized_prefix)]
    return max(scores, default=None)
