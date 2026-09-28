"""单元：三套实现交叉比对 + 金标向量具体值断言。

实现方：
1. app.core.proof.verify        —— 被测内核验证器
2. app.offline.verifier         —— 独立离线验证器（不导入 core）
3. tests/reference/NaiveSMT     —— 朴素第三方参考（只依赖 hashlib）
"""
from __future__ import annotations

import random

import pytest

from app.api.proof_envelope import proof_to_envelope
from app.core.proof import verify as core_verify
from app.core.smt import SparseMerkleTree
from app.core.store import InMemoryStore
from app.offline.verifier import verify_envelope
from app.diagnostics import Decision, Reason
from tests.reference.naive_smt import NaiveSMT


def _build_tree_and_map(params, items):
    tree = SparseMerkleTree(InMemoryStore(), params)
    root = params.empty_root
    mapping: dict[bytes, bytes] = {}
    for key, value in items:
        root = tree.update(root, key, value)
        if value is None:
            mapping.pop(key, None)
        else:
            mapping[key] = value
    return tree, root, mapping


@pytest.mark.parametrize("key_byte", [0x00, 0x05, 0x07, 0x10, 0x7F, 0x80, 0xFE, 0xFF])
def test_three_implementations_agree_on_depth8(params8, naive8, key_byte):
    items = [
        (b"\x05", b"v5"), (b"\x07", b"v7"), (b"\x00", b"v0"),
        (b"\xFF", b"vf"), (b"\x80", None),  # None 后被删除（自始不存在）
    ]
    tree, root, mapping = _build_tree_and_map(params8, items)
    naive_root = naive8.root_of(mapping)
    assert root == naive_root

    key = bytes([key_byte])
    pf = tree.prove(root, key)
    envelope = proof_to_envelope(pf, params8)

    # 1) 内核验证
    expect_membership = key in mapping
    ok_core, reason_core, _ = core_verify(pf, params8)
    assert ok_core

    # 2) 离线独立验证器
    v = verify_envelope(envelope, expect_membership=expect_membership,
                        expect_value=mapping.get(key))
    assert v.decision is Decision.ACCEPT
    assert v.reason in (Reason.MEMBERSHIP_VERIFIED, Reason.NON_MEMBERSHIP_VERIFIED)

    # 3) 朴素参考出证并自验
    npf = naive8.prove(mapping, key)
    assert naive8.verify(naive_root, key, npf)
    # 且朴素兄弟序列与内核压缩路径实体层一致
    assert npf.siblings == list(pf.siblings)
    assert npf.end == pf.end


def test_randomized_cross_implementation_agreement(params256):
    """256 位下随机 20 键（含长公共前缀簇）的批量交叉验证。"""
    rng = random.Random(4242)
    clusters = [bytes([c]) * 20 for c in range(5)]
    keys: list[bytes] = []
    for cluster in clusters:
        for _ in range(4):
            keys.append(cluster + rng.randbytes(12))
    # 去重
    keys = list(dict.fromkeys(keys))
    items = [(k, rng.randbytes(10)) for k in keys]

    tree, root, mapping = _build_tree_and_map(params256, items)
    naive = NaiveSMT(32, 256)
    assert root == naive.root_of(mapping)

    sample = rng.sample(keys, 8) + [rng.randbytes(32) for _ in range(3)]
    for key in sample:
        pf = tree.prove(root, key)
        v = verify_envelope(proof_to_envelope(pf, params256),
                            expect_membership=key in mapping,
                            expect_value=mapping.get(key))
        assert v.decision is Decision.ACCEPT, (key.hex(), v.reason, v.message)


def test_golden_vectors_concrete_values(golden_vectors):
    """断言金标中的具体哈希值（而非仅“能调用”）。"""
    g = golden_vectors
    assert g["schema"] == "smt-golden/v1"
    # 这些具体常量由独立朴素参考生成；若协议改动，金标必须刻意更新
    assert g["small_depth8"]["empty_root"] == (
        "3ea6f9e35613e296ca126f7869634edd58ea4215b5bfba3d3c47e1e489746bc5"
    )
    assert g["full_depth256"]["root"] == (
        "64ab1fb9c956572842d09e52eaed687d7a2ba6fce1357f6b564e9c14f9509859"
    )
    assert g["full_depth256"]["shared_prefix_bits"] == 255
    assert g["full_depth256"]["levels_a"] == 256


def test_offline_and_core_reject_identical_tamper_categories(params8):
    """内核与离线验证器对同一批篡改给出同一失败类别。"""
    items = [(b"\x05", b"v5"), (b"\x07", b"v7"), (b"\x00", b"v0")]
    tree, root, mapping = _build_tree_and_map(params8, items)

    pf = tree.prove(root, b"\x05")
    env = proof_to_envelope(pf, params8)

    # 改根
    tampered = {**env, "root": ("00" * 32)}
    v = verify_envelope(tampered, expect_membership=True)
    assert v.decision is Decision.REJECT and v.reason is Reason.ROOT_MISMATCH
    assert core_verify(
        type(pf)(**{**pf.__dict__, "root": b"\x00" * 32}), params8
    )[1] == "ROOT_MISMATCH"

    # 改信封内叶值 -> 叶摘要变 -> ROOT_MISMATCH
    tampered_val = {**env, "value": "ffff"}
    v = verify_envelope(tampered_val, expect_membership=True)
    assert v.reason is Reason.ROOT_MISMATCH

    # 用真证明但声明错误的期望值 -> VALUE_MISMATCH（值绑定失败）
    v = verify_envelope(env, expect_membership=True, expect_value=b"wrong-expected")
    assert v.decision is Decision.REJECT and v.reason is Reason.VALUE_MISMATCH

    # 改 bitmap 为非规范
    tampered_bm = {**env, "bitmap": "01"}  # 只有层 7 置位，前缀有洞
    v = verify_envelope(tampered_bm)
    assert v.decision is Decision.REJECT and v.reason is Reason.ENVELOPE_MALFORMED

    # 多余字段 -> 无法判定（材料格式问题）
    tampered_extra = {**env, "evil": "x"}
    v = verify_envelope(tampered_extra)
    assert v.decision is Decision.INCONCLUSIVE
    assert v.reason is Reason.ENVELOPE_MALFORMED


def test_offline_envelope_malformed_returns_inconclusive():
    for garbage in [{}, {"schema": "wrong"}, [], "not-json-like", {"schema": "smt-proof/v1"}]:
        v = verify_envelope(garbage)
        assert v.decision is Decision.INCONCLUSIVE
        assert v.reason is Reason.ENVELOPE_MALFORMED
