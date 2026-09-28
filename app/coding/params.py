"""树参数（固定键宽）。"""
from __future__ import annotations

from dataclasses import dataclass

from .hashing import empty_subtree_schedule


@dataclass(frozen=True)
class TreeParams:
    key_len: int  # 键的固定字节宽（默认 32 字节 / 256 位）
    depth: int    # 树深（位数，不超过 key_len*8）

    def __post_init__(self) -> None:
        if self.key_len <= 0:
            raise ValueError("key_len 必须为正")
        if not 0 < self.depth <= self.key_len * 8:
            raise ValueError(f"depth={self.depth} 超出键位宽 {self.key_len * 8}")
        # 空子树摘要表只算一次（frozen dataclass 下借 object.__setattr__ 缓存）
        object.__setattr__(self, "_empty", empty_subtree_schedule(self.depth))

    @property
    def empty(self) -> list[bytes]:
        """empty[d]：深度 d 处空子树的摘要；长度 depth+1。"""
        return self._empty  # type: ignore[attr-defined]

    @property
    def empty_root(self) -> bytes:
        return self.empty[0]


DEFAULT_PARAMS = TreeParams(key_len=32, depth=256)
