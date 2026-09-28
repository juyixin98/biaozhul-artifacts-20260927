"""核心 OT 测试：硬编码的具体期望结果（参考答案不是被测实现生成的）。

每个用例同时断言：变换后操作的**精确组件序列**与两个重放顺序的收敛文本。
"""
from __future__ import annotations

import pytest

from app.models import Component, Op
from app.ot import apply, transform
from tests.helpers import op_to_tuples, tuple_to_op


def t(name, base, ta, tb, exp_aprime, exp_bprime, exp_text):
    a, b = tuple_to_op(ta), tuple_to_op(tb)
    ap, bp = transform(a, b)
    assert op_to_tuples(ap) == exp_aprime, (name, op_to_tuples(ap))
    assert op_to_tuples(bp) == exp_bprime, (name, op_to_tuples(bp))
    text_via_b = apply(ap, apply(b, base))
    text_via_a = apply(bp, apply(a, base))
    assert text_via_b == exp_text, (name, text_via_b)
    assert text_via_a == exp_text, (name, text_via_a)


def test_same_point_inserts_sorted_by_origin():
    # 两个客户端在 "ab" 的位置 1 并发插入；("c1",1) < ("c2",1)
    t(
        "same-point",
        "ab",
        [("r", 1), ("i", "X", ("c1", 1)), ("r", 1)],
        [("r", 1), ("i", "Y", ("c2", 1)), ("r", 1)],
        # a' 作用于 "aYb"：在 Y 前插 X，再越过 Yb
        [("r", 1), ("i", "X", ("c1", 1)), ("r", 2)],
        # b' 作用于 "aXb"：越过 aX，在其后插 Y
        [("r", 2), ("i", "Y", ("c2", 1)), ("r", 1)],
        "aXYb",
    )


def test_same_point_inserts_order_reversed_client_ids():
    # origin 字典序相反：c2 的键更小，排前面
    t(
        "same-point-rev",
        "ab",
        [("r", 1), ("i", "X", ("c9", 1)), ("r", 1)],
        [("r", 1), ("i", "Y", ("c1", 1)), ("r", 1)],
        [("r", 2), ("i", "X", ("c9", 1)), ("r", 1)],
        [("r", 1), ("i", "Y", ("c1", 1)), ("r", 2)],
        "aYXb",
    )


def test_overlapping_deletes_not_double_charged():
    # a 删 [0,2)，b 删 [1,3)；基线 "abcd"
    # 公共结果只保留 "d"；a' 与 b' 都不应重复删除重叠区
    t(
        "overlap-delete",
        "abcd",
        [("d", 2), ("r", 2)],
        [("r", 1), ("d", 2), ("r", 1)],
        # after_b="ad"（长度2）：a 删基线{0,1}，'a' 仍在→删除它，保留 'd'
        [("d", 1), ("r", 1)],
        # after_a="cd"（长度2）：b 删基线{1,2}，1 已删；'c'(基线2)仍在→删，保留 'd'
        [("d", 1), ("r", 1)],
        "d",
    )


def test_identical_deletes_become_noops():
    # 双方删除完全相同的区间（未删光文档）：对侧为纯 retain 的 no-op
    a = Op.build([Component.delete(2), Component.retain(2)])
    b = Op.build([Component.delete(2), Component.retain(2)])
    ap, bp = transform(a, b)
    assert all(c.is_retain for c in ap.components)
    assert all(c.is_retain for c in bp.components)
    # a' 作用于 after_b="cd"（长度2），b' 作用于 after_a="cd"（长度2）
    assert ap.base_len == bp.base_len == 2
    assert apply(ap, apply(b, "abcd")) == "cd"
    assert apply(bp, apply(a, "abcd")) == "cd"

    # 双方删光整个文档：对侧为空操作（作用于长度 0 的文本，合法）
    a2 = Op.build([Component.delete(1)])
    b2 = Op.build([Component.delete(1)])
    ap2, bp2 = transform(a2, b2)
    assert ap2.components == () and bp2.components == ()
    assert apply(ap2, apply(b2, "x")) == ""
    assert apply(bp2, apply(a2, "x")) == ""


def test_delete_inside_concurrent_insert_preserves_intent():
    # a 删除 "abc" 中的 "b"；b 在位置 1 插入 "XY"
    # 意图保持：b 的插入不被 a 删掉；公共结果 "aXYc"
    t(
        "delete-vs-insert-inside",
        "abc",
        [("r", 1), ("d", 1), ("r", 1)],
        [("r", 1), ("i", "XY", ("c2", 3)), ("r", 2)],
        # after_b="aXYc"（长度4）：a' 越过 aXY，删除 'b'，保留末尾 'c'
        [("r", 3), ("d", 1), ("r", 1)],
        # after_a="ac"（长度2）：b' 在位置1插 XY，保留 c
        [("r", 1), ("i", "XY", ("c2", 3)), ("r", 1)],
        "aXYc",
    )


def test_insert_around_concurrent_delete():
    # a 在位置 2（"c"前）插入；b 删除整个 "bc"
    t(
        "insert-at-deleted-boundary",
        "abcd",
        [("r", 2), ("i", "Z", ("c1", 5)), ("r", 2)],
        [("r", 1), ("d", 2), ("r", 1)],
        # after_b="ad"：a 的锚点(原位置2)处 b,c 已删，Z 锚定在 a 之后
        [("r", 1), ("i", "Z", ("c1", 5)), ("r", 1)],
        # after_a="abZcd"：b 要删基线 b、c，二者仍在（Z 在它们之间）
        [("r", 1), ("d", 1), ("r", 1), ("d", 1), ("r", 1)],
        "aZd",
    )


def test_multibyte_unicode_indices():
    # 中文 + emoji：码点索引。"你好🌟ab"，长度按码点 = 5
    base = "你好🌟ab"
    assert len(base) == 5
    a = Op.insert_at(2, "世", ("c1", 1), len(base))
    b = Op.insert_at(2, "界", ("c2", 1), len(base))
    ap, bp = transform(a, b)
    assert apply(ap, apply(b, base)) == apply(bp, apply(a, base)) == "你好世界🌟ab"


def test_apply_rejects_out_of_range_and_bad_length():
    from app.errors import MalformedOperation
    # delete_range 构造期即拒绝越界
    with pytest.raises(MalformedOperation):
        Op.delete_range(2, 3, 4)  # [2,5) > 4
    # apply 拒绝基线长度不符
    with pytest.raises(MalformedOperation):
        apply(Op.build([Component.retain(1)]), "abc")
    # insert 位置越界
    with pytest.raises(MalformedOperation):
        Op.insert_at(9, "x", ("c", 1), 2)


def test_normalization_rules():
    # 相邻 retain/delete 合并；不同源 insert 不合并；末尾 retain 保留
    op = Op.build([
        Component.retain(1), Component.retain(2),
        Component.insert("a", ("c1", 1)),
        Component.insert("b", ("c2", 1)),
        Component.delete(1), Component.delete(2),
        Component.retain(3),
    ])
    kinds = [(c.kind, c.value) for c in op.components]
    assert kinds == [
        ("retain", 3),
        ("insert", "a"),
        ("insert", "b"),
        ("delete", 3),
        ("retain", 3),
    ]
    # 同源相邻 insert 合并
    op2 = Op.build([
        Component.insert("a", ("c1", 1)),
        Component.insert("b", ("c1", 1)),
        Component.retain(1),
    ])
    assert [(c.kind, c.value) for c in op2.components] == [
        ("insert", "ab"), ("retain", 1),
    ]
    # 纯 retain（no-op）在 Op 层允许（并发重叠删除的对侧结果），
    # 客户端提交 no-op 编辑由服务层拒绝（见 service/API 测试）。
    noop = Op.build([Component.retain(3)])
    assert noop.is_noop()
