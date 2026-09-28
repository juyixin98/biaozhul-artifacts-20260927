"""单元：内核证明数学——长公共前缀、非成员、压缩展开、删除复原、空值语义。"""
from __future__ import annotations


from app.api.proof_envelope import proof_to_envelope
from app.core.proof import (
    END_EMPTY,
    END_LEAF,
    END_WRONG_KEY,
    LogicalProof,
    popcount_bitmap,
    verify,
    V_KEY_BINDING_MISMATCH,
    V_KIND_MISMATCH,
    V_MALFORMED,
    V_ROOT_MISMATCH,
    V_VALUE_MISMATCH,
)


# ---------- 长公共前缀（两键长公共前缀）----------

def test_two_key_long_common_prefix_compressed_proof(params256, tree8):
    from app.core.smt import SparseMerkleTree
    from app.core.store import InMemoryStore

    tree = SparseMerkleTree(InMemoryStore(), params256)
    ka = b"\x11" * 31 + b"\x00"
    kb = b"\x11" * 31 + b"\x01"  # 与 ka 共享 255 位，仅末位不同
    root = tree.update(params256.empty_root, ka, b"alpha")
    root = tree.update(root, kb, b"beta")

    for key, value in ((ka, b"alpha"), (kb, b"beta")):
        pf = tree.prove(root, key)
        assert pf.end == END_LEAF
        # 压缩表示：256 个实体分支层（共享 255 位单边链 + 末位分歧）
        assert popcount_bitmap(pf.bitmap, 256) == 256
        assert len(pf.siblings) == 256
        # 单边链层的兄弟都是逐层空摘要，证明“压缩但可展开”
        for d in range(255):
            assert pf.siblings[d] == params256.empty[d + 1]
        # 第 256 个兄弟（分歧层）是另一叶
        assert pf.siblings[255] == tree.prove(root, kb if key == ka else ka).siblings[255] \
            or pf.siblings[255] != params256.empty[256]
        ok, reason, recomputed = verify(pf, params256)
        assert ok, f"成员证明核验失败: {reason}"
        assert recomputed == root


def test_long_common_prefix_matches_naive_reference(params256, golden_vectors):
    """与独立朴素参考实现生成的金标根/证明逐字节一致。"""
    gv = golden_vectors["full_depth256"]
    from app.core.smt import SparseMerkleTree
    from app.core.store import InMemoryStore

    tree = SparseMerkleTree(InMemoryStore(), params256)
    ka, kb = bytes.fromhex(gv["key_a"]), bytes.fromhex(gv["key_b"])
    root = tree.update(tree.update(params256.empty_root, ka, b"alpha"), kb, b"beta")
    assert root.hex() == gv["root"]
    pf = tree.prove(root, ka)
    env = proof_to_envelope(pf, params256)
    assert env == gv["proof_a"]  # 信封逐字段等于金标


def test_depth8_two_keys_share_five_prefix_bits(tree8, naive8, params8):
    """0x05=00000101 与 0x07=00000111 共享前 6 位，在第 7 层（depth=6）分歧。"""
    root = tree8.update(tree8.update(params8.empty_root, b"\x05", b"v5"), b"\x07", b"v7")
    # 朴素参考：从集合直接算根
    assert root == naive8.root_of({b"\x05": b"v5", b"\x07": b"v7"})
    pf = tree8.prove(root, b"\x05")
    # 层 0..5 为两键共享的单边链，层 6 为分歧分支 -> 0..6 共 7 个实体层
    assert popcount_bitmap(pf.bitmap, 8) == 7
    assert pf.bitmap.hex() == "fe"
    # 前 6 层（0..5）单边：兄弟是空摘要；层 6 分歧，兄弟是 0x07 的叶摘要
    for d in range(6):
        assert pf.siblings[d] == params8.empty[d + 1]
    assert pf.siblings[6] != params8.empty[7]
    ok, reason, _ = verify(pf, tree8.params)
    assert ok and reason == "OK"
    # 朴素出证的兄弟序列一致
    npf = naive8.prove({b"\x05": b"v5", b"\x07": b"v7"}, b"\x05")
    assert [s for s in pf.siblings] == npf.siblings


# ---------- 非成员证明 ----------

def test_non_membership_empty_tree(tree8, params8):
    pf = tree8.prove(params8.empty_root, b"\x42")
    assert pf.end == END_EMPTY
    assert pf.siblings == []
    ok, reason, recomputed = verify(pf, params8)
    assert ok
    assert recomputed == params8.empty_root


def test_non_membership_collision_leaf(tree8, params8):
    root = tree8.update(params8.empty_root, b"\x05", b"v5")
    pf = tree8.prove(root, b"\x06")  # 0110 vs 0101：层 5 分歧
    assert pf.end == END_WRONG_KEY
    assert pf.collision_key == b"\x05"
    assert pf.collision_value == b"v5"
    ok, reason, _ = verify(pf, params8)
    assert ok, reason


def test_non_membership_empty_slot_below_branch(tree8, params8):
    # {00, 01, ff}：右侧 d0 只有 ff（上浮叶于 d1）；左侧 d1 是 {00,01} 分支，
    # 查 0x02(00000010) 走到左侧 d1 分支后，层 2 进入空槽 -> END_EMPTY。
    root = params8.empty_root
    for k in (b"\x00", b"\x01", b"\xff"):
        root = tree8.update(root, k, b"v")
    pf = tree8.prove(root, b"\x02")
    assert pf.end == END_EMPTY
    ok, reason, _ = verify(pf, params8)
    assert ok, reason


def test_non_membership_floated_other_leaf_is_wrong_key(tree8, params8):
    # {00, ff} 下查 0x80：d0 右孩子是 ff 的上浮叶（占据整个右子树槽）-> wrong_key
    root = tree8.update(tree8.update(params8.empty_root, b"\x00", b"a"), b"\xff", b"b")
    pf = tree8.prove(root, b"\x80")
    assert pf.end == END_WRONG_KEY
    assert pf.collision_key == b"\xff"
    ok, reason, _ = verify(pf, params8)
    assert ok, reason
    assert tree8.get_value(root, b"\x80") is None


# ---------- 压缩路径篡改（断言具体失败类别）----------

def _flip_byte(data: bytes, index: int = 0) -> bytes:
    out = bytearray(data)
    out[index] ^= 0x01
    return bytes(out)


def test_tamper_root_gives_root_mismatch(tree8, params8):
    root = tree8.update(params8.empty_root, b"\x05", b"v5")
    pf = tree8.prove(root, b"\x05")
    bad = LogicalProof(**{**pf.__dict__, "root": _flip_byte(pf.root)})
    ok, reason, recomputed = verify(bad, params8)
    assert not ok
    assert reason == V_ROOT_MISMATCH
    assert recomputed == root  # 重算根恰好是真根，诊断可指出差异


def test_tamper_value_gives_value_mismatch(tree8, params8):
    root = tree8.update(params8.empty_root, b"\x05", b"v5")
    pf = tree8.prove(root, b"\x05")
    ok, reason, _ = verify(pf, params8, expect_value=b"forged")
    assert not ok and reason == V_VALUE_MISMATCH


def test_tamper_leaf_value_inside_proof_gives_root_mismatch(tree8, params8):
    root = tree8.update(params8.empty_root, b"\x05", b"v5")
    pf = tree8.prove(root, b"\x05")
    bad = LogicalProof(**{**pf.__dict__, "value": b"v6"})
    ok, reason, _ = verify(bad, params8)
    assert not ok and reason == V_ROOT_MISMATCH  # 叶摘要变 -> 重算根不符


def test_tamper_collision_key_prefix_binding(tree8, params8):
    # {05,07} 下查 06(00000110)：沿 06 路径到达 05 或 07 的上浮叶（levels=7）。
    root = tree8.update(tree8.update(params8.empty_root, b"\x05", b"v5"), b"\x07", b"v7")
    pf = tree8.prove(root, b"\x06")  # 非成员，碰撞叶沿其路径
    assert pf.end == END_WRONG_KEY
    # 换成一个前 levels 位不匹配的键 -> KEY_BINDING_MISMATCH（而非根不符）
    forged = b"\xff"
    assert forged != pf.collision_key
    bad = LogicalProof(**{**pf.__dict__, "collision_key": forged})
    ok, reason, _ = verify(bad, params8)
    assert not ok and reason == V_KEY_BINDING_MISMATCH


def test_tamper_compressed_path_sibling_gives_root_mismatch(tree8, params8):
    root = tree8.update(tree8.update(params8.empty_root, b"\x05", b"v5"), b"\x07", b"v7")
    pf = tree8.prove(root, b"\x05")
    tampered_siblings = list(pf.siblings)
    tampered_siblings[2] = _flip_byte(tampered_siblings[2])
    bad = LogicalProof(**{**pf.__dict__, "siblings": tampered_siblings})
    ok, reason, _ = verify(bad, params8)
    assert not ok and reason == V_ROOT_MISMATCH


def test_non_canonical_bitmap_rejected(tree8, params8):
    root = tree8.update(params8.empty_root, b"\x05", b"v5")
    pf = tree8.prove(root, b"\x05")
    # 手工构造“中间有洞”的非法 bitmap：层 2 置位但层 0/1 不置位
    bogus = bytearray(pf.bitmap)
    bogus[0] = 0x20  # 仅第 5 位
    bad = LogicalProof(
        key=pf.key, root=root, end=END_LEAF, bitmap=bytes(bogus),
        siblings=[pf.siblings[0]] if pf.siblings else [b"\x00" * 32],
        value=b"v5",
    )
    ok, reason, _ = verify(bad, params8)
    assert not ok and reason == V_MALFORMED


def test_kind_mismatch_cases(tree8, params8):
    root = tree8.update(params8.empty_root, b"\x05", b"v5")
    pf_member = tree8.prove(root, b"\x05")
    pf_absent = tree8.prove(params8.empty_root, b"\x05")

    # leaf 证明却带 collision_key
    bad = LogicalProof(**{**pf_member.__dict__, "collision_key": b"\x06"})
    assert verify(bad, params8)[1] == V_KIND_MISMATCH
    # empty 证明却带 value
    bad2 = LogicalProof(**{**pf_absent.__dict__, "value": b""})
    assert verify(bad2, params8)[1] == V_KIND_MISMATCH


def test_bitmap_padding_bits_must_be_zero(params8):
    # depth=8 恰好 1 字节无填充；用 depth=5 的参数制造 3 个填充位
    p5 = type(params8)(key_len=1, depth=5)
    from app.core.smt import SparseMerkleTree
    from app.core.store import InMemoryStore
    t = SparseMerkleTree(InMemoryStore(), p5)
    root = t.update(p5.empty_root, b"\x05", b"v")
    pf = t.prove(root, b"\x05")
    bad_bm = bytearray(pf.bitmap)
    bad_bm[0] |= 0x01  # 填充位（第 8 位）置 1
    bad = LogicalProof(**{**pf.__dict__, "bitmap": bytes(bad_bm)})
    assert verify(bad, p5)[1] == V_MALFORMED


# ---------- 删除复原 ----------

def test_delete_then_restore_returns_exact_prior_root(tree8, params8):
    root = tree8.update(tree8.update(params8.empty_root, b"\x05", b"v5"), b"\x07", b"v7")
    after_delete = tree8.update(root, b"\x05", None)
    assert tree8.get_value(after_delete, b"\x05") is None
    restored = tree8.update(after_delete, b"\x05", b"v5")
    assert restored == root  # 删除复原后根与删除前完全一致


def test_delete_last_key_back_to_empty_root(tree8, params8):
    root = tree8.update(params8.empty_root, b"\x05", b"v5")
    assert tree8.update(root, b"\x05", None) == params8.empty_root


def test_delete_absent_key_is_noop(tree8, params8):
    root = tree8.update(params8.empty_root, b"\x05", b"v5")
    assert tree8.update(root, b"\x06", None) == root


def test_empty_byte_value_distinct_from_absent(tree8, params8):
    root = tree8.update(params8.empty_root, b"\x05", b"")  # 空值
    assert tree8.get_value(root, b"\x05") == b""           # 存在
    assert tree8.get_value(root, b"\x06") is None          # 不存在
    pf_present = tree8.prove(root, b"\x05")
    pf_absent = tree8.prove(root, b"\x06")
    assert pf_present.end == END_LEAF and pf_present.value == b""
    assert pf_absent.end in (END_EMPTY, END_WRONG_KEY)
    assert verify(pf_present, params8)[0]
    assert verify(pf_absent, params8)[0]
