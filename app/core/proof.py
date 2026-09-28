"""逻辑证明（不含线格式/信封）与纯函数核验。

紧凑 SMT 语义（内容寻址存储）：
* 深度 d 的空子树摘要为 ``params.empty[d]``（逐层派生）。
* 只含一个键的子树，其根摘要直接是叶摘要——叶“上浮”到该层。
* 含两个及以上键的子树实体化为分支；单边分支（一侧为空 E[d+1]）允许存在，
  且删除后只要另一孩子是叶就把叶提升，保证**相同键值集合必产生相同根**。

压缩证明表示：
* ``bitmap``：定长 ``depth`` 位，位=1 表示该层路径经过实体分支；规范编码下
  置位层必为严格前缀 {0,1,...,L-1}，L 即被证槽的“上浮层”。
* ``siblings``：L 条 32 字节摘要，按层号升序；单边分支处对应条目就是 E[d+1]。
  验证器凭此即可逐层展开，无需服务端补数据（“压缩路径必须可展开验证”）。

证明三种终止类型：
* ``leaf``：成员证明，起点是 leaf_digest(key, value)，叶内绑定键与值；
* ``empty``：非成员，起点是空摘要 E[L]；
* ``wrong_key``：非成员，沿被证键路径到达了另一个键的上浮叶；要求该叶键的
  前 L 个路径位与被证键一致（前缀绑定），否则 KEY_BINDING_MISMATCH。

注意：核验函数只依赖 app.coding。离线包 app/offline/verifier.py 不导入本模块，
而是独立实现同一数学逻辑，由测试交叉比对——防止参考答案全部由被测实现生成。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.coding.hashing import HASH_SIZE, branch_digest, leaf_digest
from app.coding.keys import bit_at
from app.coding.params import TreeParams

END_EMPTY = "empty"
END_LEAF = "leaf"
END_WRONG_KEY = "wrong_key"

# 核验失败类别（与 app.diagnostics.Reason 对齐，core 层不引入 API 枚举）
VERIFY_OK = "OK"
V_ROOT_MISMATCH = "ROOT_MISMATCH"
V_VALUE_MISMATCH = "VALUE_MISMATCH"
V_KEY_BINDING_MISMATCH = "KEY_BINDING_MISMATCH"
V_DEPTH_MISMATCH = "DEPTH_MISMATCH"
V_KIND_MISMATCH = "KIND_MISMATCH"
V_MALFORMED = "MALFORMED"


@dataclass(frozen=True)
class LogicalProof:
    key: bytes
    root: bytes
    end: str                            # END_EMPTY / END_LEAF / END_WRONG_KEY
    bitmap: bytes                       # ceil(depth/8) 字节
    siblings: list[bytes]               # L 条 32 字节摘要，按层号升序
    value: bytes | None = None          # end=leaf 时的叶值
    collision_key: bytes | None = None  # end=wrong_key 时的实际叶键
    collision_value: bytes | None = None

    @property
    def is_membership(self) -> bool:
        return self.end == END_LEAF


# ---------- bitmap 工具 ----------

def empty_bitmap(depth: int) -> bytearray:
    return bytearray((depth + 7) // 8)


def set_bit(bitmap: bytearray, depth_level: int) -> None:
    bitmap[depth_level // 8] |= 1 << (7 - (depth_level % 8))


def bit_is_set(bitmap: bytes, depth_level: int) -> bool:
    return bool((bitmap[depth_level // 8] >> (7 - (depth_level % 8))) & 1)


def popcount_bitmap(bitmap: bytes, depth: int) -> int:
    return sum(1 for d in range(depth) if bit_is_set(bitmap, d))


def bitmap_valid(bitmap: bytes, params: TreeParams) -> bool:
    if len(bitmap) != (params.depth + 7) // 8:
        return False
    # 尾部填充位必须为 0：同一证明不得有两种编码（防可锻性）
    for d in range(params.depth, len(bitmap) * 8):
        if bit_is_set(bitmap, d):
            return False
    return True


def prefix_levels(bitmap: bytes, params: TreeParams) -> int | None:
    """置位层为严格前缀 {0..L-1} 时返回 L，否则返回 None（非规范压缩）。"""
    n = popcount_bitmap(bitmap, params.depth)
    for d in range(n):
        if not bit_is_set(bitmap, d):
            return None
    for d in range(n, params.depth):
        if bit_is_set(bitmap, d):
            return None
    return n


# ---------- 纯函数核验 ----------

def verify(
    proof: LogicalProof,
    params: TreeParams,
    expect_value: bytes | None = None,
) -> tuple[bool, str, bytes | None]:
    """核验逻辑证明，返回 ``(是否通过, 失败类别, 重算根)``。

    ROOT_MISMATCH / VALUE_MISMATCH 时尽量返回重算根，便于诊断对比；
    结构性错误（无法构造起点）时重算根为 None。
    """
    if len(proof.key) != params.key_len or len(proof.root) != HASH_SIZE:
        return False, V_MALFORMED, None
    if not bitmap_valid(proof.bitmap, params):
        return False, V_MALFORMED, None
    if any(len(s) != HASH_SIZE for s in proof.siblings):
        return False, V_MALFORMED, None

    levels = prefix_levels(proof.bitmap, params)
    if levels is None:
        return False, V_MALFORMED, None
    if len(proof.siblings) != levels:
        return False, V_MALFORMED, None
    if levels > params.depth:
        return False, V_DEPTH_MISMATCH, None

    if proof.end == END_LEAF:
        if proof.value is None or proof.collision_key is not None:
            return False, V_KIND_MISMATCH, None
        node_hash = leaf_digest(proof.key, proof.value)  # 叶内绑定键+值
        if expect_value is not None and proof.value != expect_value:
            recomputed = _climb(node_hash, proof.key, levels, proof.siblings)
            return False, V_VALUE_MISMATCH, recomputed

    elif proof.end == END_EMPTY:
        if proof.value is not None or proof.collision_key is not None:
            return False, V_KIND_MISMATCH, None
        node_hash = params.empty[levels]  # 上浮空槽

    elif proof.end == END_WRONG_KEY:
        ck, cv = proof.collision_key, proof.collision_value
        if ck is None or cv is None or proof.value is not None:
            return False, V_KIND_MISMATCH, None
        if len(ck) != params.key_len or ck == proof.key:
            return False, V_KEY_BINDING_MISMATCH, None
        # 路径前缀绑定：碰撞叶是沿被证键的前 L 个分支位走到的，
        # 它的键在这些层必须与被证键同路径。
        if any(bit_at(ck, d) != bit_at(proof.key, d) for d in range(levels)):
            return False, V_KEY_BINDING_MISMATCH, None
        node_hash = leaf_digest(ck, cv)

    else:
        return False, V_MALFORMED, None

    recomputed = _climb(node_hash, proof.key, levels, proof.siblings)
    if recomputed != proof.root:
        return False, V_ROOT_MISMATCH, recomputed
    return True, VERIFY_OK, recomputed


def _climb(start_hash: bytes, key: bytes, levels: int, siblings: list[bytes]) -> bytes:
    """从上浮层 L 向根逐层展开压缩路径。"""
    node_hash = start_hash
    for d in range(levels - 1, -1, -1):
        sibling = siblings[d]
        if bit_at(key, d) == 0:
            node_hash = branch_digest(d, node_hash, sibling)
        else:
            node_hash = branch_digest(d, sibling, node_hash)
    return node_hash
