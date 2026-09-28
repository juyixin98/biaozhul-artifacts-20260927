"""单元：确定化批处理——批更新与逐条更新最终根一致、规范化规则。"""
from __future__ import annotations

import random


from app.core.batch import (
    apply_batch,
    apply_one_by_one,
    canonical_batch_id,
    normalize_batch,
)
from app.core.smt import SparseMerkleTree
from app.core.store import InMemoryStore
from tests.reference.naive_smt import NaiveSMT


def _rng() -> random.Random:
    return random.Random(20260928)


def _keys256(n: int, rng: random.Random) -> list[bytes]:
    # 故意制造多组长公共前缀：同一 16 字节前缀下生成多个键
    keys = []
    for i in range(n):
        prefix = bytes([i % 4]) * 16
        keys.append(prefix + rng.randbytes(16))
    return keys


def test_normalize_last_write_wins_and_sorted(params256):
    k1 = b"\x02" + b"\x00" * 31
    k2 = b"\x01" + b"\x00" * 31
    updates = normalize_batch(
        [(k1, b"first"), (k2, b"x"), (k1, b"last")], params256.key_len
    )
    assert [u.key for u in updates] == [k2, k1]       # 排序
    assert updates[1].value == b"last"                # 后者覆盖


def test_batch_id_is_deterministic(params256):
    k = b"\x00" * 32
    a = normalize_batch([(k, b"v")], params256.key_len)
    b = normalize_batch([(k, b"v")], params256.key_len)
    assert canonical_batch_id(a) == canonical_batch_id(b)
    c = normalize_batch([(k, b"w")], params256.key_len)
    assert canonical_batch_id(a) != canonical_batch_id(c)


def test_batch_root_equals_one_by_one_small_tree(params8):
    """比较批更新与逐个更新的最终根（小参数、具体键）。"""
    rng = _rng()
    keys = [bytes([v]) for v in (0x00, 0x05, 0x07, 0x10, 0x11, 0x7F, 0x80, 0xFF)]
    rng.shuffle(keys)
    items = [(k, f"val-{k.hex()}".encode()) for k in keys]

    t_batch = SparseMerkleTree(InMemoryStore(), params8)
    updates = normalize_batch(items, params8.key_len)
    batch_root, changed = apply_batch(t_batch, params8.empty_root, updates)

    t_seq = SparseMerkleTree(InMemoryStore(), params8)
    seq_root = apply_one_by_one(t_seq, params8.empty_root, items)

    assert batch_root == seq_root
    assert changed == len(keys)

    # 朴素参考（从集合直接算）也是同一个根
    naive = NaiveSMT(key_len=1, depth=8)
    assert batch_root == naive.root_of(dict(items))


def test_batch_root_equals_one_by_one_full_depth_random(params256):
    """全尺寸 256 位、随机操作（含改值与删除）下批根 == 逐条根 == 朴素集合根。"""
    rng = _rng()
    keys = _keys256(12, rng)
    items: list[tuple[bytes, bytes | None]] = [(k, rng.randbytes(8)) for k in keys]
    # 追加重复键（顺序乱序、后者覆盖）和删除项
    items.append((keys[0], b"overwritten"))
    items.append((keys[3], None))
    rng.shuffle(items)

    t_batch = SparseMerkleTree(InMemoryStore(), params256)
    updates = normalize_batch(items, params256.key_len)
    batch_root, _ = apply_batch(t_batch, params256.empty_root, updates)

    t_seq = SparseMerkleTree(InMemoryStore(), params256)
    seq_root = apply_one_by_one(t_seq, params256.empty_root, items)
    assert batch_root == seq_root

    # 朴素集合根（手动应用 last-write-wins 与删除）
    expected_map: dict[bytes, bytes] = {}
    for k, v in items:
        if v is None:
            expected_map.pop(k, None)
        else:
            expected_map[k] = v
    naive = NaiveSMT(32, 256)
    assert batch_root == naive.root_of(expected_map)


def test_multiple_batches_chain_deterministically(params256):
    """同一集合分多批到达，最终根与一批到达相同。"""
    rng = _rng()
    keys = _keys256(8, rng)
    all_items = [(k, rng.randbytes(4)) for k in keys]

    t_one = SparseMerkleTree(InMemoryStore(), params256)
    root_one, _ = apply_batch(t_one, params256.empty_root,
                              normalize_batch(all_items, params256.key_len))

    t_multi = SparseMerkleTree(InMemoryStore(), params256)
    root_multi = params256.empty_root
    for i in range(0, len(all_items), 3):
        chunk = all_items[i:i + 3]
        root_multi, _ = apply_batch(
            t_multi, root_multi, normalize_batch(chunk, params256.key_len)
        )
    assert root_multi == root_one


def test_empty_batch_keeps_root(params256):
    tree = SparseMerkleTree(InMemoryStore(), params256)
    new_root, changed = apply_batch(tree, params256.empty_root, [])
    assert new_root == params256.empty_root and changed == 0
