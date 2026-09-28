"""确定化批处理规则。

规则（对调用方与离线回放完全一致，且可独立重算）：
1. 同批内同键出现多次 -> 后者覆盖（last-write-wins）。
2. 按键字节序升序排序后顺序应用（规范化批次）。
3. 规范化批次为空 -> 根不变，batch_id 仍记录（空批是合法操作）。

由于每个单点更新只依赖键集合语义（结构坍缩规则保证），批内顺序其实不影响
最终根；但“排序 + 后者覆盖”给出唯一规范化序列，保证日志、batch_id 与跨实现
重放逐字节一致。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.coding.keys import normalize_key, normalize_value
from app.coding.serialization import canonical_json
from .smt import SparseMerkleTree


@dataclass(frozen=True)
class Update:
    key: bytes
    value: bytes | None  # None=删除；b""=存在且值为空

    def to_journal(self) -> dict:
        return {"key": self.key.hex(), "value": None if self.value is None else self.value.hex()}


def normalize_batch(items: list[tuple[bytes, bytes | None]], key_len: int) -> list[Update]:
    """规范化一批 (key, value)：定宽校验、后者覆盖、按键排序。"""
    merged: dict[bytes, bytes | None] = {}
    for raw_key, raw_value in items:
        key = normalize_key(raw_key, key_len)
        value = normalize_value(raw_value)
        merged[key] = value  # 后者覆盖
    return [Update(key=k, value=merged[k]) for k in sorted(merged.keys())]


def canonical_batch_id(updates: list[Update]) -> str:
    """由规范化批次内容派生确定性 batch_id（幂等/审计/去重用）。"""
    payload = canonical_json([u.to_journal() for u in updates])
    return hashlib.sha256(b"batch-v1:" + payload).hexdigest()


def apply_batch(
    tree: SparseMerkleTree, root: bytes, updates: list[Update]
) -> tuple[bytes, int]:
    """顺序应用规范化批次，返回 ``(新根, 实际变更条数)``。

    实际变更条数 = 应用前后值发生变化（含删除）的键数，供审计诊断使用。
    """
    changed = 0
    for u in updates:
        before = tree.get_value(root, u.key)
        new_root = tree.update(root, u.key, u.value)
        after = tree.get_value(new_root, u.key) if new_root != root else before
        if before != after:
            changed += 1
        root = new_root
    return root, changed


def apply_one_by_one(
    tree: SparseMerkleTree, root: bytes, items: list[tuple[bytes, bytes | None]]
) -> bytes:
    """对照路径：逐条（不批处理、不规范化排序）应用原始序列。

    集成测试用它与 apply_batch 比较最终根，验证“批更新 == 逐条更新”。
    """
    for key, value in items:
        root = tree.update(root, key, value)
    return root
