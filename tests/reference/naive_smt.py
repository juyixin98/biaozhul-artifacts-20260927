"""第三方独立参考实现：朴素稀疏 Merkle 树（仅用 hashlib，不导入 app.core）。

这是“参考答案不由被测核心自身生成”的关键保障：本文件用最直白的方式
（全键集合递归重建 + 显式上浮判定）独立实现与 app.core.smt 相同的协议语义，
哈希标签常量在此重新声明（它们是协议规范的一部分，而非被测代码）。

若被测内核与本朴素实现对任意随机操作序列都产生相同根与互相认可的证明，
则两份独立代码互为印证。scripts/gen_golden_vectors.py 用本实现固化金标。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

# —— 协议固定常量（与 app/coding/hashing.py 的标签对应，但在此独立声明）——
TAG_LEAF = b"\x00"
TAG_BRANCH = b"\x01"
TAG_EMPTY = b"\x02"


def _h(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _leaf_hash(key: bytes, value: bytes) -> bytes:
    return _h(TAG_LEAF + len(key).to_bytes(2, "big") + key + value)


def _branch_hash(depth: int, left: bytes, right: bytes) -> bytes:
    assert len(left) == 32 and len(right) == 32
    return _h(TAG_BRANCH + depth.to_bytes(2, "big") + left + right)


@dataclass(frozen=True)
class _NLeaf:
    key: bytes
    value: bytes

    @property
    def digest(self) -> bytes:
        return _leaf_hash(self.key, self.value)


@dataclass(frozen=True)
class _NBranch:
    depth: int
    left: object
    right: object
    digest: bytes


class NaiveSMT:
    """朴素递归 SMT。节点对象即叶或分支；空子树没有节点对象。

    结构规则（独立表述，不看被测代码）：
    * 空子树根 = 空摘要 E[depth]，E 由叶层向上逐层派生；
    * 单子树“上浮”：depth 层若整棵子树只有一个键，则该层根就是叶摘要，
      不实体化分支；单边分支只出现在共享前缀上，链底是真正的双叶分歧。
    """

    def __init__(self, key_len: int = 32, depth: int = 256) -> None:
        self.key_len = key_len
        self.depth = depth
        self._empties = [b""] * (depth + 1)
        self._empties[depth] = _h(TAG_EMPTY)
        for d in range(depth - 1, -1, -1):
            self._empties[d] = _branch_hash(d, self._empties[d + 1], self._empties[d + 1])

    @property
    def empty_root(self) -> bytes:
        return self._empties[0]

    def empty_at(self, depth: int) -> bytes:
        return self._empties[depth]

    def _bit(self, key: bytes, depth: int) -> int:
        return (key[depth // 8] >> (7 - depth % 8)) & 1

    def root_of(self, mapping: dict[bytes, bytes]) -> bytes:
        """直接由键值集合计算根（不经过任何“更新序列”），杜绝顺序依赖。"""
        if not mapping:
            return self.empty_root
        return self._build(0, mapping).digest

    def _build(self, depth: int, mapping: dict[bytes, bytes]):
        if not mapping:
            raise AssertionError("空映射应由调用方短路")
        if len(mapping) == 1:
            (key, value), = mapping.items()
            return _NLeaf(key, value)
        zeros, ones = {}, {}
        for key, value in mapping.items():
            (zeros if self._bit(key, depth) == 0 else ones)[key] = value
        left = self._build(depth + 1, zeros) if zeros else None
        right = self._build(depth + 1, ones) if ones else None
        lh = left.digest if left is not None else self._empties[depth + 1]
        rh = right.digest if right is not None else self._empties[depth + 1]
        return _NBranch(depth, left, right, _branch_hash(depth, lh, rh))

    @dataclass
    class NaiveProof:
        key: bytes
        end: str                 # 'leaf' / 'empty' / 'wrong_key'
        levels: int
        siblings: list           # list[bytes]，按层号升序（单边层为空摘要）
        value: bytes | None
        collision_key: bytes | None
        collision_value: bytes | None

    def prove(self, mapping: dict[bytes, bytes], key: bytes) -> "NaiveSMT.NaiveProof":
        return self._prove(0, mapping, key, [])

    def _prove(self, depth, mapping, key, siblings_acc):
        if not mapping:
            return self.NaiveProof(key, "empty", depth, list(siblings_acc),
                                   None, None, None)
        if len(mapping) == 1:
            (k, v), = mapping.items()
            if k == key:
                return self.NaiveProof(key, "leaf", depth, list(siblings_acc),
                                       v, None, None)
            return self.NaiveProof(key, "wrong_key", depth, list(siblings_acc),
                                   None, k, v)
        # 按被证键的位选同侧
        b = self._bit(key, depth)
        same = {k: v for k, v in mapping.items() if self._bit(k, depth) == b}
        other = {k: v for k, v in mapping.items() if self._bit(k, depth) != b}
        other_hash = self._build(depth + 1, other).digest if other else self._empties[depth + 1]
        siblings_acc.append(other_hash)
        return self._prove(depth + 1, same, key, siblings_acc)

    def verify(self, mapping_root: bytes, key: bytes, pf: "NaiveSMT.NaiveProof") -> bool:
        """第三份独立核验实现（仅供测试交叉比对）。"""
        if pf.end == "leaf":
            start = _leaf_hash(key, pf.value)
        elif pf.end == "empty":
            start = self._empties[pf.levels]
        else:
            ck = pf.collision_key
            if ck is None or ck == key:
                return False
            if any(self._bit(ck, d) != self._bit(key, d) for d in range(pf.levels)):
                return False
            start = _leaf_hash(ck, pf.collision_value)
        h = start
        for d in range(pf.levels - 1, -1, -1):
            sib = pf.siblings[d]
            h = _branch_hash(d, h, sib) if self._bit(key, d) == 0 else _branch_hash(d, sib, h)
        return h == mapping_root
