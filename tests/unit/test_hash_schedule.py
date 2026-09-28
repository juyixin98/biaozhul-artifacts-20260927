"""单元：空节点逐层哈希、叶内绑定键值、域分离与定宽校验。"""
from __future__ import annotations

import pytest

from app.coding.hashing import (
    branch_digest,
    empty_leaf_digest,
    empty_subtree_schedule,
    leaf_digest,
)
from app.coding.keys import bit_at, normalize_key
from app.coding.params import TreeParams


def test_empty_schedule_is_defined_layer_by_layer(params8):
    table = empty_subtree_schedule(8)
    assert len(table) == 9
    # 叶层空槽直接定义
    assert table[8] == empty_leaf_digest()
    # 其余每层由下一层的一对空子树摘要派生
    for d in range(8):
        assert table[d] == branch_digest(d, table[d + 1], table[d + 1])
    # 空根 = table[0]
    assert params8.empty_root == table[0]


def test_empty_value_leaf_differs_from_absent_empty_slot(params8):
    """不存在（空槽）与存在但值为空字节串必须有不同摘要。"""
    empty_slot = empty_leaf_digest()
    present_with_empty_value = leaf_digest(b"\x00", b"")
    assert empty_slot != present_with_empty_value


def test_leaf_digest_binds_key_and_value():
    # 值不同 -> 不同摘要
    assert leaf_digest(b"\x01", b"a") != leaf_digest(b"\x01", b"b")
    # 键不同 -> 不同摘要（即使值相同）
    assert leaf_digest(b"\x01", b"a") != leaf_digest(b"\x02", b"a")
    # 空值与非空值不同
    assert leaf_digest(b"\x01", b"") != leaf_digest(b"\x01", b"\x00")


def test_domain_separation_blocks_node_type_confusion():
    """标签域分离：叶/分支/空槽摘要之间不应意外相等。"""
    tag = empty_leaf_digest()
    leaf = leaf_digest(tag[:1], tag[1:])  # 即便载荷字节相同
    assert tag != leaf
    assert tag != branch_digest(0, tag, tag)
    assert leaf != branch_digest(0, leaf, leaf)


def test_branch_digest_binds_depth():
    a = leaf_digest(b"\x00", b"a")
    b = leaf_digest(b"\x01", b"b")
    assert branch_digest(3, a, b) != branch_digest(4, a, b)
    assert branch_digest(3, a, b) != branch_digest(3, b, a)  # 左右有序


def test_branch_digest_rejects_short_children():
    with pytest.raises(ValueError):
        branch_digest(0, b"short", b"x" * 32)


def test_fixed_key_width_enforced(params8):
    assert normalize_key("05", 1) == b"\x05"
    with pytest.raises(ValueError):
        normalize_key("0506", 1)   # 太宽
    with pytest.raises(ValueError):
        normalize_key("zz", 1)     # 非 hex


def test_depth_must_fit_key_width():
    TreeParams(key_len=1, depth=8)
    with pytest.raises(ValueError):
        TreeParams(key_len=1, depth=9)
    with pytest.raises(ValueError):
        TreeParams(key_len=0, depth=0)


def test_bit_ordering_msb_first():
    key = b"\x05"  # 00000101
    assert [bit_at(key, i) for i in range(8)] == [0, 0, 0, 0, 0, 1, 0, 1]
