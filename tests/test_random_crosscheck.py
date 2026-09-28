"""随机化交叉验证：操作序列同时作用于被测 Trie+Engine 与独立参考模型。

每一步后都做两件事：

1. 对随机前缀、随机 k 的查询，结果必须与“全量筛选再排序”的朴素答案
   **逐项相等**（顺序也要一致 —— 精确 top-k，而不是集合相等）；
2. Trie 结构/上界不变量必须为空违规；特别验证热词降权后根上界等于
   参考模型里的真实全局最高分（上界不陈旧）。

随机种子写入日志，失败时可用固定种子精确复现。
"""

from __future__ import annotations

import random

import pytest

from app.trie import RadixTrie, TopKTrace
from tests.reference import ReferenceStore, ref_normalize

ALPHABET = [
    "a", "b", "c", "ab", "abc", "inter", "intern", "cafe",
    "ＣＡＦＥ", "CAFE", "ﬁ", "Ω", "ω", "あ", "ア",
]


def _random_word(rng: random.Random) -> str:
    n = rng.randint(1, 3)
    return "".join(rng.choice(ALPHABET) for _ in range(n))


@pytest.mark.parametrize("seed", [20260927, 17, 90210, 424242, 7])
def test_random_mutations_against_reference(seed, run_id, log):
    rng = random.Random(seed)
    log.info("[%s] 随机交叉验证开始 seed=%d", run_id, seed)
    t = RadixTrie()
    ref = ReferenceStore()

    next_serial = 0

    def fresh_id() -> str:
        nonlocal next_serial
        next_serial += 1
        return f"e{next_serial:04d}"

    existing_ids: list[str] = []

    def sync_assert(prefix_raw: str, k: int, step: int) -> None:
        # Trie 层接收已规范化的前缀（规范化职责在 engine）；这里与
        # 参考预言使用同一个独立规范化函数。
        pfx_norm = ref_normalize(prefix_raw)
        trace = TopKTrace(prefix_norm="", matched=False, node_id=None)
        got = t.top_k(pfx_norm, k, trace=trace)
        want = ref.ref_top_k(prefix_raw, k)
        got_s = [(r[0], r[1], r[2], r[3]) for r in got]
        assert got_s == want, (
            f"失败类别: 随机交叉 top-k 不一致 seed={seed} step={step} "
            f"prefix={prefix_raw!r} norm={pfx_norm!r} k={k}\n got={got_s}\nwant={want}"
        )

    for step in range(400):
        roll = rng.random()
        if roll < 0.55 or not existing_ids:
            # 插入新 id
            eid = fresh_id()
            word = _random_word(rng)
            score = rng.randint(0, 20)
            t.upsert(eid, ref_normalize(word), word, float(score))
            ref.upsert(eid, word, score)
            existing_ids.append(eid)
        elif roll < 0.80:
            # 词频更新/热词降权：同 id 改分（可能同时改 display，也可能不改）
            eid = rng.choice(existing_ids)
            old = ref.entries[eid]
            if rng.random() < 0.3:
                word = _random_word(rng)
            else:
                word = old.display
            score = rng.randint(0, 20)
            t.upsert(eid, ref_normalize(word), word, float(score))
            ref.upsert(eid, word, score)
        else:
            # 删除
            eid = rng.choice(existing_ids)
            old = ref.entries[eid]
            assert t.delete(eid, old.term_norm) is True, (
                f"失败类别: 参考模型存在的词条在 Trie 中删除失败 seed={seed} step={step}"
            )
            ref.delete(eid)
            existing_ids.remove(eid)

        # 每步校验不变量
        violations = t.check_invariants()
        assert not violations, (
            f"失败类别: 变更后不变量违规 seed={seed} step={step}: {violations[:3]}"
        )
        # 上界可靠性：根 max_score 必须等于参考模型真实最高分
        if existing_ids:
            real_max = max(e.score for e in ref.entries.values())
            assert t.root.max_score == real_max, (
                f"失败类别: 根上界陈旧 seed={seed} step={step} "
                f"cached={t.root.max_score} real={real_max}"
            )
        else:
            assert t.root.max_score == float("-inf")

        # 每步对随机查询做精确比对
        q = _random_word(rng) if rng.random() < 0.7 else ""
        sync_assert(q, rng.randint(1, 6), step)

    # 收尾：一批固定查询 + 剪枝统计日志
    for pfx in ["", "a", "inter", "cafe", "abc", "zzz"]:
        trace = TopKTrace(prefix_norm="", matched=False, node_id=None)
        got = t.top_k(pfx, 5, trace=trace)
        want = ref.ref_top_k(pfx, 5)
        assert [(r[0], r[1], r[2], r[3]) for r in got] == want
        log.info(
            "[%s] seed=%d 收尾查询 prefix=%r 命中=%d 展开节点=%d 整枝剪枝=%d 终止词条扫描=%d",
            run_id, seed, pfx, len(got), trace.expanded,
            trace.pruned_children, trace.terminals_seen,
        )
    log.info("[%s] 随机交叉验证通过 seed=%d 总词条=%d", run_id, seed, len(existing_ids))
