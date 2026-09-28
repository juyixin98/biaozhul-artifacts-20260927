"""候选索引与剪枝下界。

下界必须与代价模型一致（admissible：永不超过真实距离），否则会漏掉阈值内
候选。测试用穷举对拍保证这一点。提供两个下界并取较大者：

1. 长度差下界：每个 insert/delete 至多改变长度 1，取最便宜的 insert 或
   delete 单价（transpose/substitute 不改变长度）。
       LB_len = |len(q)-len(c)| * min(w_ins, w_del)

2. 字符计数 L1 下界：设 Δ = Σ|multiset_c - multiset_q|。
   - substitute / transpose 对 Δ 贡献至多 2（transpose 只在两个字符互不相同时）；
   - insert / delete 贡献 1。
   为使下界对“任意代价组合”都成立，使用最便宜的有效操作单价：
       LB_cnt = ceil0(Δ) 意义下，配对部分按 min(2*min_indel, min_sub/trans_pair)，
                未配对部分按 min_indel。
   这里采用保守但严格的实现：
       unmatched  = Δ（计数差总量，需由 insert/delete 消化）
       但配对可让两个 unmatched 合并为一次 substitute 或 transpose，
       每次配对最多“消化”2 单位 Δ。
   令 p = min(Σ_c min(qcnt_c, ccnt_c 反向缺口), ...) 简化为：
       pairs = (Σ min(qcnt, ccnt) 的互补统计) —— 见代码中按“正负差”配对。
       LB_cnt = pairs * min(2*min_indel, min_cost) + (Δ - 2*pairs) * min_indel
   其中 min_cost 是 substitute（含字符对表）/transpose 的最便宜单价；
   pairs 为可配对数上限 = min(Σ正差, Σ负差) = Δ/2。
   因此化简为：
       LB_cnt = (Δ//2) * min(2*min_indel, min_pair_cost) + (Δ%2) * min_indel
   配对用 substitute 还是 transpose 不影响“至多消化 2 单位 Δ”的事实。
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass

from .costs import CostProfile


@dataclass(frozen=True)
class IndexEntry:
    word: str
    freq: int
    length: int


class LexiconIndex:
    """内存索引：词条按长度分桶、桶内按词排序（稳定、确定）。"""

    def __init__(self, version_id: str, entries: list[tuple[str, int]]):
        self.version_id = version_id
        ordered = sorted(entries, key=lambda kv: kv[0])
        self._entries: list[IndexEntry] = [
            IndexEntry(word=w, freq=f, length=len(w)) for w, f in ordered
        ]
        by_length: dict[int, list[IndexEntry]] = {}
        for e in self._entries:
            by_length.setdefault(e.length, []).append(e)
        self._by_length = dict(sorted(by_length.items()))
        self._lengths = sorted(self._by_length.keys())
        self._counters = {e.word: Counter(e.word) for e in self._entries}

    @property
    def size(self) -> int:
        return len(self._entries)

    def length_bucket_candidates(self, query_length: int, threshold: float,
                                 min_indel: float) -> list[IndexEntry]:
        """长度差下界过滤：保留 |Δlen| * min_indel <= threshold 的长度桶。"""
        if min_indel == 0.0:
            allowed = set(self._lengths)
        else:
            span = int(math.floor(threshold / min_indel + 1e-12))
            allowed = {
                L for L in self._lengths if abs(L - query_length) <= span
            }
        out: list[IndexEntry] = []
        for L in allowed:
            out.extend(self._by_length[L])
        out.sort(key=lambda e: e.word)
        return out

    def all_entries(self) -> list[IndexEntry]:
        return list(self._entries)

    def counter_of(self, word: str) -> Counter:
        return self._counters[word]


def lower_bound_length(query: str, candidate: str, profile: CostProfile) -> float:
    return abs(len(query) - len(candidate)) * profile.min_indel_cost()


def lower_bound_counts(query: str, candidate: str, profile: CostProfile) -> float:
    q, c = Counter(query), Counter(candidate)
    delta = 0
    for ch in q.keys() | c.keys():
        delta += abs(q.get(ch, 0) - c.get(ch, 0))

    min_indel = profile.min_indel_cost()
    # 能一次消化 2 单位计数差的操作只有 substitute / transpose；
    # insert/delete 每次只能消化 1 单位（走两次 insert+delete 需 2*min_indel）。
    table_values = [
        float(v)
        for row in profile.substitute_table.values()
        for v in row.values()
    ]
    pair_op_cost = min(profile.substitute, profile.transpose, *table_values)
    pairs, leftover = divmod(delta, 2)
    per_pair = min(2.0 * min_indel, pair_op_cost)
    return pairs * per_pair + leftover * min_indel


def combined_lower_bound(query: str, candidate: str, profile: CostProfile) -> float:
    return max(
        lower_bound_length(query, candidate, profile),
        lower_bound_counts(query, candidate, profile),
    )
