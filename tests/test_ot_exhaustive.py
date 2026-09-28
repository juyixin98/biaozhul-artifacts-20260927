"""穷举/抽样测试：用独立预言机（原子打标）校验生产 transform。

参考答案由 ``tests.oracle``（不 import 任何 app 代码）生成，生产核心只负责
与之相等——绝不用生产实现自己生成期望。

覆盖：
  * 短串上的全部"原始编辑"操作对，检查两个 prong 的精确组件与收敛文本；
  * 一批三操作序列：全部 6 种接收顺序经服务式重放后收敛到同一文本；
  * 多字节字符串（中文/emoji）以码点索引参与穷举。
"""
from __future__ import annotations

import itertools

import pytest

from app.ot import apply, transform
from tests.helpers import op_to_tuples, tuple_to_op
from tests.oracle import oracle_transform


def all_primitive_edits(text: str, client_id: str):
    """生成一段文本上一个客户端可发出的全部"单意图"原始操作（元组形式）。

    每个位置插入一个短串；每个 [pos,pos+n) 区间删除。全部带末尾 retain，
    覆盖整个基线。
    """
    L = len(text)
    inserts = ["x", "z"] if all(ord(ch) < 128 for ch in text) else ["甲"]
    seq = 0
    for pos in range(L + 1):
        for payload in inserts:
            seq += 1
            op = []
            if pos:
                op.append(("r", pos))
            op.append(("i", payload, (client_id, seq)))
            if L - pos:
                op.append(("r", L - pos))
            yield tuple(op)
    for pos in range(L):
        for n in range(1, L - pos + 1):
            op = []
            if pos:
                op.append(("r", pos))
            op.append(("d", n))
            if L - pos - n:
                op.append(("r", L - pos - n))
            yield tuple(op)


@pytest.mark.parametrize("base", ["", "a", "ab", "abc"])
def test_all_primitive_pairs_matches_oracle(base, otlog):
    edits_a = list(all_primitive_edits(base, "ca"))
    edits_b = list(all_primitive_edits(base, "cb"))
    checked = 0
    first_mismatch_ctx = None
    for ta, tb in itertools.product(edits_a, edits_b):
        a, b = tuple_to_op(list(ta)), tuple_to_op(list(tb))
        exp_a, exp_b, exp_text = oracle_transform(base, list(ta), list(tb))

        ap, bp = transform(a, b)
        got_a, got_b = op_to_tuples(ap), op_to_tuples(bp)
        if got_a != exp_a or got_b != exp_b:
            first_mismatch_ctx = {"ta": ta, "tb": tb,
                                  "got_a": got_a, "exp_a": exp_a,
                                  "got_b": got_b, "exp_b": exp_b}
        assert got_a == exp_a, (base, ta, tb, got_a, exp_a)
        assert got_b == exp_b, (base, ta, tb, got_b, exp_b)

        assert apply(ap, apply(b, base)) == exp_text
        assert apply(bp, apply(a, base)) == exp_text
        checked += 1
    otlog(
        "oracle-pairs",
        verdict="pass",
        reason=f"穷举 {checked} 个操作对，生产 transform 与独立预言机逐组件相等，"
               "且两个重放顺序文本收敛",
        inputs={"base": base, "pairs_checked": checked,
                "edits_per_side": len(edits_a)},
        states={"final_oracle_text_sample": exp_text},
    )
    assert checked > 0


# 多字节：码点索引。"你🌟" 长度 2；在其上做完整操作对穷举
@pytest.mark.parametrize("base", ["你🌟", "a你"])
def test_multibyte_pairs_matches_oracle(base):
    edits_a = list(all_primitive_edits(base, "ca"))
    edits_b = list(all_primitive_edits(base, "cb"))
    for ta, tb in itertools.product(edits_a, edits_b):
        exp_a, exp_b, exp_text = oracle_transform(base, list(ta), list(tb))
        ap, bp = transform(tuple_to_op(list(ta)), tuple_to_op(list(tb)))
        assert op_to_tuples(ap) == exp_a, (base, ta, tb, op_to_tuples(ap))
        assert op_to_tuples(bp) == exp_b, (base, ta, tb, op_to_tuples(bp))
        assert apply(ap, apply(tuple_to_op(list(tb)), base)) == exp_text
        assert apply(bp, apply(tuple_to_op(list(ta)), base)) == exp_text


def _server_replay(base: str, ops_in_order):
    """模拟服务端：所有操作都声称基于 rev 0，按给定到达顺序依次 transform。"""
    head_ops: list = []  # 已落库的（变换后）操作
    text = base
    for incoming in ops_in_order:
        rebased = incoming
        for landed in head_ops:
            rebased, _ = transform(rebased, landed)
        head_ops.append(rebased)
        text = apply(rebased, text)
    return text


def test_triple_sequences_all_orders_converge(otlog):
    """三操作序列：固定三个不同客户端的编辑，穷举 6 种到达顺序，全部收敛。"""
    base = "abc"
    triples = [
        (  # 同点三插入
            [("r", 1), ("i", "P", ("c1", 1)), ("r", 2)],
            [("r", 1), ("i", "Q", ("c2", 1)), ("r", 2)],
            [("r", 1), ("i", "R", ("c3", 1)), ("r", 2)],
        ),
        (  # 插入 + 两个不同删除
            [("r", 2), ("i", "Z", ("c1", 2)), ("r", 1)],
            [("d", 2), ("r", 1)],
            [("r", 1), ("d", 2)],
        ),
        (  # 三处不同位置插入
            [("i", "H", ("c1", 1)), ("r", 3)],
            [("r", 3), ("i", "T", ("c2", 1))],
            [("r", 1), ("i", "M", ("c3", 1)), ("r", 2)],
        ),
        (  # 三方删除互相重叠
            [("d", 2), ("r", 1)],
            [("r", 1), ("d", 2)],
            [("d", 1), ("r", 1), ("d", 1)],
        ),
    ]
    for tri in triples:
        ops = [tuple_to_op(list(o)) for o in tri]
        order_results = {}
        for order in itertools.permutations(range(3)):
            ordered = [ops[i] for i in order]
            order_results[order] = _server_replay(base, ordered)
        results = set(order_results.values())
        assert len(results) == 1, (tri, order_results)
        final = next(iter(results))
        surviving = "".join(
            ch for i, ch in enumerate(base) if not _is_deleted_by_any(i, tri)
        )
        inserted_chars = "".join(c[1] for o in tri for c in o if c[0] == "i")
        assert len(final) == len(surviving) + len(inserted_chars)
        # 存活字符相对顺序保持（子序列，非子串）
        _assert_subsequence(final, surviving)
        # 每个插入串都完整出现
        for o in tri:
            for c in o:
                if c[0] == "i":
                    assert c[1] in final
        otlog(
            "triple-orders",
            verdict="pass",
            reason="三操作的全部 6 种到达顺序经服务式 transform 重放得到同一文本",
            inputs={"base": base,
                    "ops": [list(o) for o in tri],
                    "orders_checked": list(order_results.keys())},
            states={"converged_text": final,
                    "per_order": {"->".join(map(str, k)): v
                                  for k, v in order_results.items()}},
        )


def _assert_subsequence(text: str, sub: str) -> None:
    it = iter(text)
    assert all(ch in it for ch in sub)


def _is_deleted_by_any(base_idx: int, triple) -> bool:
    for op in triple:
        i = 0
        for comp in op:
            if comp[0] == "r":
                i += comp[1]
            elif comp[0] == "d":
                if i <= base_idx < i + comp[1]:
                    return True
                i += comp[1]
    return False
