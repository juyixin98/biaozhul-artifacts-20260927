"""摘要寻址的节点存储协议与内存实现。

SQLite 实现位于 app.storage；离线回放与单元测试使用内存实现。
存储只负责字节级读写，不知道树语义。
"""
from __future__ import annotations

from typing import Protocol

from app.coding.nodes import BranchNode, LeafNode


class NodeStore(Protocol):
    def get(self, digest: bytes) -> LeafNode | BranchNode | None: ...

    def put_leaf(self, node: LeafNode) -> None:
        """按内容摘要写入（重复写幂等）。"""

    def put_branch(self, node: BranchNode) -> None:
        """按内容摘要写入（重复写幂等）。"""


class InMemoryStore:
    """dict 支持的存储；重复写入同摘要节点是无害的幂等操作。"""

    def __init__(self) -> None:
        self._nodes: dict[bytes, LeafNode | BranchNode] = {}

    def get(self, digest: bytes) -> LeafNode | BranchNode | None:
        return self._nodes.get(digest)

    def put_leaf(self, node: LeafNode) -> None:
        existing = self._nodes.get(node.digest)
        if existing is not None and existing != node:
            raise AssertionError("同摘要叶节点内容冲突（SHA-256 碰撞，不应发生）")
        self._nodes[node.digest] = node

    def put_branch(self, node: BranchNode) -> None:
        digest = node.digest_at()
        existing = self._nodes.get(digest)
        if existing is not None and existing != node:
            raise AssertionError("同摘要分支内容冲突（SHA-256 碰撞，不应发生）")
        self._nodes[digest] = node

    def __len__(self) -> int:
        return len(self._nodes)

    def contains(self, digest: bytes) -> bool:
        return digest in self._nodes
