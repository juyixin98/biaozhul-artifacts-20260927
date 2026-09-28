"""持久化节点的逻辑表示。

节点只有两种：叶（绑定键值）与分支（绑定深度与左右两条定长子摘要）。
空槽不是节点——沿路径遇到空孩子时用 ``params.empty[depth + 1]`` 表达。
叶可上浮到任意层：只含一个键的子树，根摘要即叶摘要。
"""
from __future__ import annotations

from dataclasses import dataclass

from .hashing import branch_digest, leaf_digest

# 持久化类型标记
KIND_LEAF = "leaf"
KIND_BRANCH = "branch"


@dataclass(frozen=True)
class LeafNode:
    key: bytes
    value: bytes

    @property
    def kind(self) -> str:
        return KIND_LEAF

    @property
    def digest(self) -> bytes:
        return leaf_digest(self.key, self.value)

    def tap_put(self, store) -> bytes:
        """写入内容寻址存储并返回自身摘要。"""
        store.put_leaf(self)
        return self.digest


@dataclass(frozen=True)
class BranchNode:
    depth: int
    left: bytes   # 左子摘要，恒为 32 字节；空左子取 params.empty[depth+1]
    right: bytes  # 右子摘要，恒为 32 字节；空右子取 params.empty[depth+1]

    def __post_init__(self) -> None:
        if len(self.left) != 32 or len(self.right) != 32:
            raise ValueError("分支左右孩子必须为 32 字节摘要")

    @property
    def kind(self) -> str:
        return KIND_BRANCH

    def digest_at(self) -> bytes:
        return branch_digest(self.depth, self.left, self.right)
