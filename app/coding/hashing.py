"""哈希原语（成熟密码库：Python 标准 hashlib 提供的 SHA-256）。

域分离（domain separation）至关重要——三类节点绝不允许产生可互换的摘要：

* 叶节点 ``LEAF``：叶内同时绑定键和值。空值叶与“空槽”使用不同标签，
  因此**不存在**（空槽）与**存在但值为空字节串**严格区分，不会混同。
* 内部节点 ``BRANCH``：绑定左右两条**定长 32 字节**子摘要**以及深度**。
  空孩子不是“省略”，而是代入该深度的空子树摘要 E[depth+1]，
  因此独立验证器仅凭空摘要表即可展开任意压缩路径。
* 空槽 ``EMPTY``：仅在叶层定义，再逐层向上派生出各深度的空子树摘要。

叶可以“上浮”：只含一个键的子树，其根摘要直接就是叶摘要（与所在层无关）；
含两个及以上键的子树才实体化为分支。这是标准紧凑 SMT 语义。
"""
from __future__ import annotations

import hashlib

# 单字节域标签，永久固定；更改标签等于更换哈希方案。
TAG_LEAF = 0x00
TAG_BRANCH = 0x01
TAG_EMPTY = 0x02

HASH_SIZE = 32
_DEPTH_BYTES = 2


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def leaf_digest(key: bytes, value: bytes) -> bytes:
    """叶摘要：显式绑定键与值（值可以为空字节串，仍表示“存在”）。"""
    return sha256(bytes([TAG_LEAF]) + len(key).to_bytes(2, "big") + key + value)


def branch_digest(depth: int, left: bytes, right: bytes) -> bytes:
    """内部节点摘要：绑定深度与左右两条定长子摘要。

    ``left`` / ``right`` 必须都是 32 字节：实孩子传其节点摘要，
    空孩子传 ``params.empty[depth + 1]``。深度入摘要，杜绝跨层换节点。
    """
    if len(left) != HASH_SIZE or len(right) != HASH_SIZE:
        raise ValueError("分支孩子摘要必须为定长 32 字节（空孩子用空摘要表代入）")
    return sha256(bytes([TAG_BRANCH]) + depth.to_bytes(_DEPTH_BYTES, "big") + left + right)


def empty_leaf_digest() -> bytes:
    """叶层空槽摘要（标签与叶节点不同，不绑定任何键值）。"""
    return sha256(bytes([TAG_EMPTY]))


def empty_subtree_schedule(depth: int) -> list[bytes]:
    """返回长度 ``depth + 1`` 的表：``table[d]`` 是深度 d 处空子树的摘要。

    ``table[depth]`` 是叶层空槽；``table[d] = branch(d, table[d+1], table[d+1])``
    逐层向上。这正是“空节点哈希逐层定义”的要求。
    """
    table = [b""] * (depth + 1)
    table[depth] = empty_leaf_digest()
    for d in range(depth - 1, -1, -1):
        table[d] = branch_digest(d, table[d + 1], table[d + 1])
    return table
