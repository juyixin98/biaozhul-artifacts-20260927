"""固定键宽稀疏 Merkle 树（链状态内核）。

规范结构（保证确定性根，与更新顺序、批/逐条方式无关）：
1. 空孩子以逐层空摘要 ``params.empty[depth+1]`` 表达，分支孩子恒为 32B 摘要。
2. 单子树上浮：只含一个键的子树，其根摘要就是该叶摘要（叶可停在任意层）。
3. 递归更新：
   * 空槽插入 -> 上浮叶；空槽删除 -> 无操作；
   * 叶与新键同键 -> 改值/删除；异键 -> 在首个分歧层分裂，共享前缀铺单边链；
   * 分支 -> 递归对应孩子后重绑；重绑时若只剩一个叶孩子则提升叶。
4. 因此任一“键值集合”对应唯一结构与唯一根。

存储为内容寻址（append-only，重复写幂等），旧根引用的旧节点永不物理删除
——历史证明在旧根上永远可核验。
"""
from __future__ import annotations

from app.coding.keys import bit_at, first_divergence, normalize_key, normalize_value
from app.coding.nodes import BranchNode, LeafNode
from app.coding.params import DEFAULT_PARAMS, TreeParams
from .errors import StoreCorruption
from .proof import (
    END_EMPTY,
    END_LEAF,
    END_WRONG_KEY,
    LogicalProof,
    empty_bitmap,
    set_bit,
)
from .store import NodeStore


class SparseMerkleTree:
    def __init__(self, store: NodeStore, params: TreeParams = DEFAULT_PARAMS) -> None:
        self.store = store
        self.params = params

    # ---------- 读 ----------

    def root_for_empty(self) -> bytes:
        return self.params.empty_root

    def get_value(self, root: bytes, key: bytes) -> bytes | None:
        """沿路径查找当前值；不存在返回 None（绝不与空字节串值混同）。"""
        key = normalize_key(key, self.params.key_len)
        node_hash = root
        for depth in range(self.params.depth + 1):
            if node_hash == self.params.empty[depth]:
                return None  # 进入空子树
            node = self.store.get(node_hash)
            if node is None:
                raise StoreCorruption(f"深度 {depth} 节点 {node_hash.hex()[:16]}… 缺失")
            if isinstance(node, LeafNode):
                if node.key != key:
                    return None  # 槽位被异键上浮叶占据：被证键不存在
                return node.value
            child = node.left if bit_at(key, depth) == 0 else node.right
            node_hash = child
        raise StoreCorruption("超过树深未到达叶")

    def update(self, root: bytes, key: bytes, value: bytes | None) -> bytes:
        """单点更新/删除，返回新根。value=None 表示删除。"""
        key = normalize_key(key, self.params.key_len)
        value = normalize_value(value)
        return self._update(root, key, value, 0)

    # ---------- 递归更新（规范形式）----------

    def _update(self, node_hash: bytes, key: bytes, value: bytes | None, depth: int) -> bytes:
        if node_hash == self.params.empty[depth]:
            if value is None:
                return node_hash
            leaf = LeafNode(key=key, value=value)
            self.store.put_leaf(leaf)
            return leaf.digest

        node = self.store.get(node_hash)
        if node is None:
            raise StoreCorruption(f"深度 {depth} 节点 {node_hash.hex()[:16]}… 缺失")

        if isinstance(node, LeafNode):
            if node.key == key:
                if value is None:
                    return self.params.empty[depth]
                new_leaf = LeafNode(key=key, value=value)
                self.store.put_leaf(new_leaf)
                return new_leaf.digest
            if value is None:
                return node_hash  # 异键上浮叶：目标键不存在，无操作
            if depth >= self.params.depth:
                raise StoreCorruption("不同键无法在树深内分流")
            return self._split(node, key, value, depth)

        # 分支：递归后重绑（提升规则在 _rebind 内）
        b = bit_at(key, depth)
        child = node.left if b == 0 else node.right
        new_child = self._update(child, key, value, depth + 1)
        if new_child == child:
            return node_hash
        left = new_child if b == 0 else node.left
        right = new_child if b == 1 else node.right
        return self._rebind(depth, left, right)

    def _split(self, existing: LeafNode, key: bytes, value: bytes, depth: int) -> bytes:
        """existing 上浮于 depth 层；与 key 在 div(>depth) 层分歧。

        构造 depth 层子树根：div 层双叶分支 + depth..div-1 共享前缀单边链。
        """
        div = first_divergence(existing.key, key, depth, self.params.depth)
        if div >= self.params.depth:
            raise StoreCorruption("不同键无法在树深内分流")
        new_leaf = LeafNode(key=key, value=value)
        self.store.put_leaf(new_leaf)
        if bit_at(existing.key, div) == 0:
            h = self._put_branch(div, existing.digest, new_leaf.digest)
        else:
            h = self._put_branch(div, new_leaf.digest, existing.digest)
        for d in range(div - 1, depth - 1, -1):
            if bit_at(existing.key, d) == 0:
                h = self._put_branch(d, h, self.params.empty[d + 1])
            else:
                h = self._put_branch(d, self.params.empty[d + 1], h)
        return h

    def _rebind(self, depth: int, left: bytes, right: bytes) -> bytes:
        """双空 -> 空摘要；单叶孩子 -> 叶上浮；否则实体化分支。"""
        e = self.params.empty[depth + 1]
        if left == e and right == e:
            return self.params.empty[depth]
        if left == e:
            only = self.store.get(right)
            if isinstance(only, LeafNode):
                return right
        elif right == e:
            only = self.store.get(left)
            if isinstance(only, LeafNode):
                return left
        return self._put_branch(depth, left, right)

    def _put_branch(self, depth: int, left: bytes, right: bytes) -> bytes:
        branch = BranchNode(depth=depth, left=left, right=right)
        self.store.put_branch(branch)
        return branch.digest_at()

    # ---------- 证明 ----------

    def prove(self, root: bytes, key: bytes) -> LogicalProof:
        key = normalize_key(key, self.params.key_len)
        bitmap = empty_bitmap(self.params.depth)
        siblings: list[bytes] = []
        node_hash = root

        for depth in range(self.params.depth + 1):
            if node_hash == self.params.empty[depth]:
                return LogicalProof(
                    key=key, root=root, end=END_EMPTY,
                    bitmap=bytes(bitmap), siblings=siblings,
                )
            node = self.store.get(node_hash)
            if node is None:
                raise StoreCorruption(f"证明路径深度 {depth} 节点 {node_hash.hex()[:16]}… 缺失")
            if isinstance(node, LeafNode):
                # 上浮叶即该槽子树根：它占据整个子树。异键 -> wrong_key，
                # 由验证器检查叶键的前 depth 个路径位确实与被证键一致（前缀绑定）。
                if node.key == key:
                    return LogicalProof(
                        key=key, root=root, end=END_LEAF,
                        bitmap=bytes(bitmap), siblings=siblings, value=node.value,
                    )
                return LogicalProof(
                    key=key, root=root, end=END_WRONG_KEY,
                    bitmap=bytes(bitmap), siblings=siblings,
                    collision_key=node.key, collision_value=node.value,
                )
            # 实体分支：兄弟（可能是 E[depth+1]）入压缩序列
            set_bit(bitmap, depth)
            b = bit_at(key, depth)
            siblings.append(node.right if b == 0 else node.left)
            node_hash = node.left if b == 0 else node.right

        raise StoreCorruption("证明遍历超过树深")
