"""独立离线证明验证器。

本模块不导入 app.core.proof（被测内核的证明实现），而是只拿 app.coding 的
哈希/键原语，自行实现信封解析、压缩路径展开与逐域检查。失败分类与内核一致，
测试交叉验证二者在全部夹具（含篡改）上给出相同 ACCEPT/REJECT 与 reason。

三态语义：
* 纯密码学核验只产生 ACCEPT / REJECT；
* 信封解析不了、公钥不可用等材料问题由调用方标记 INCONCLUSIVE。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.coding.hashing import HASH_SIZE, branch_digest, leaf_digest
from app.coding.keys import bit_at
from app.coding.params import TreeParams
from app.diagnostics import Decision, Reason, Verdict
from app.logging_setup import key_fingerprint

END_EMPTY = "empty"
END_LEAF = "leaf"
END_WRONG_KEY = "wrong_key"

_ALLOWED_FIELDS = {
    "schema", "key_len", "depth", "key", "root", "end",
    "bitmap", "siblings", "value", "collision_key", "collision_value",
}
PROOF_SCHEMA = "smt-proof/v1"


@dataclass
class _Parsed:
    params: TreeParams
    key: bytes
    root: bytes
    end: str
    bitmap: bytes
    siblings: list[bytes]
    value: bytes | None
    collision_key: bytes | None
    collision_value: bytes | None


def _parse_envelope(envelope: dict) -> _Parsed:
    """严格解析（与服务端解析器各自独立写一遍，逻辑互为对照）。"""
    if not isinstance(envelope, dict):
        raise ValueError("信封不是 JSON 对象")
    if set(envelope) != _ALLOWED_FIELDS:
        raise ValueError(
            f"信封字段集合不封闭：多余={sorted(set(envelope) - _ALLOWED_FIELDS)} "
            f"缺失={sorted(_ALLOWED_FIELDS - set(envelope))}"
        )
    if envelope["schema"] != PROOF_SCHEMA:
        raise ValueError(f"未知 schema: {envelope['schema']!r}")
    if envelope["end"] not in (END_EMPTY, END_LEAF, END_WRONG_KEY):
        raise ValueError(f"未知 end: {envelope['end']!r}")

    params = TreeParams(int(envelope["key_len"]), int(envelope["depth"]))
    key = bytes.fromhex(envelope["key"])
    root = bytes.fromhex(envelope["root"])
    bitmap = bytes.fromhex(envelope["bitmap"])
    siblings = [bytes.fromhex(s) for s in envelope["siblings"]]

    if len(key) != params.key_len:
        raise ValueError("键宽与 key_len 不符")
    if len(root) != HASH_SIZE:
        raise ValueError("根必须为 32 字节")
    if len(bitmap) != (params.depth + 7) // 8:
        raise ValueError("bitmap 长度与 depth 不符")
    if any(len(s) != HASH_SIZE for s in siblings):
        raise ValueError("siblings 必须均为 32 字节")

    value = None if envelope["value"] is None else bytes.fromhex(envelope["value"])
    collision_key = (
        None if envelope["collision_key"] is None
        else bytes.fromhex(envelope["collision_key"])
    )
    collision_value = (
        None if envelope["collision_value"] is None
        else bytes.fromhex(envelope["collision_value"])
    )
    return _Parsed(params, key, root, envelope["end"], bitmap, siblings,
                   value, collision_key, collision_value)


def _prefix_levels(bitmap: bytes, depth: int) -> int | None:
    """置位层必须是严格前缀 {0..L-1}；尾部填充位必须为 0。"""
    n = 0
    for d in range(depth):
        if (bitmap[d // 8] >> (7 - d % 8)) & 1:
            n += 1
    if any((bitmap[d // 8] >> (7 - d % 8)) & 1 for d in range(depth, len(bitmap) * 8)):
        return None
    for d in range(n):
        if not ((bitmap[d // 8] >> (7 - d % 8)) & 1):
            return None
    for d in range(n, depth):
        if (bitmap[d // 8] >> (7 - d % 8)) & 1:
            return None
    return n


def _expand(start: bytes, key: bytes, levels: int, siblings: list[bytes]) -> bytes:
    """独立重写的压缩路径展开：从上浮槽逐层向根。"""
    h = start
    for d in range(levels - 1, -1, -1):
        sib = siblings[d]
        if bit_at(key, d) == 0:
            h = branch_digest(d, h, sib)
        else:
            h = branch_digest(d, sib, h)
    return h


def verify_envelope(
    envelope: dict,
    *,
    expect_membership: bool | None = None,
    expect_value: bytes | None = None,
) -> Verdict:
    """核验一个证明信封。

    ``expect_membership``：调用方的成员/非成员预期；None 表示只核验自洽性。
    ``expect_value``：成员证明下要求叶值等于该值（hex str 或 bytes）。
    """
    try:
        parsed = _parse_envelope(envelope)
    except (ValueError, TypeError, KeyError) as exc:
        return Verdict(
            Decision.INCONCLUSIVE, Reason.ENVELOPE_MALFORMED,
            f"证明信封无法解析: {exc}",
        )

    p = parsed.params
    kfp = key_fingerprint(parsed.key)

    # 成员/非成员预期与 end 的一致性（把“证明类型”变成显式断言）
    if expect_membership is True and parsed.end != END_LEAF:
        return Verdict(
            Decision.REJECT, Reason.KIND_MISMATCH,
            "期望成员证明，但证明终止类型不是 leaf",
            key_fingerprint=kfp, claimed_root=envelope.get("root"),
        )
    if expect_membership is False and parsed.end == END_LEAF:
        return Verdict(
            Decision.REJECT, Reason.KIND_MISMATCH,
            "期望非成员证明，但证明终止类型是 leaf（键实际存在）",
            key_fingerprint=kfp, claimed_root=envelope.get("root"),
        )

    levels = _prefix_levels(parsed.bitmap, p.depth)
    if levels is None:
        return Verdict(
            Decision.REJECT, Reason.ENVELOPE_MALFORMED,
            "bitmap 非规范压缩编码（置位层不是严格前缀或填充位非 0）",
            key_fingerprint=kfp,
        )
    if len(parsed.siblings) != levels:
        return Verdict(
            Decision.REJECT, Reason.ENVELOPE_MALFORMED,
            f"siblings 数({len(parsed.siblings)}) 与实体分支数({levels}) 不符",
            key_fingerprint=kfp,
        )

    # —— 构造上浮层起点 ——
    if parsed.end == END_LEAF:
        if parsed.value is None or parsed.collision_key is not None:
            return Verdict(Decision.REJECT, Reason.KIND_MISMATCH,
                           "leaf 证明缺 value 或携带 collision_*", key_fingerprint=kfp)
        start = leaf_digest(parsed.key, parsed.value)  # 叶内绑定键+值
        if expect_value is not None:
            want = bytes.fromhex(expect_value) if isinstance(expect_value, str) else expect_value
            if parsed.value != want:
                recomputed = _expand(start, parsed.key, levels, parsed.siblings).hex()
                return Verdict(
                    Decision.REJECT, Reason.VALUE_MISMATCH,
                    "叶值与声明值不符",
                    key_fingerprint=kfp, claimed_root=parsed.root.hex(),
                    recomputed_root=recomputed,
                    detail={"declared": want.hex()},
                )

    elif parsed.end == END_EMPTY:
        if parsed.value is not None or parsed.collision_key is not None:
            return Verdict(Decision.REJECT, Reason.KIND_MISMATCH,
                           "empty 证明携带了 value/collision_*", key_fingerprint=kfp)
        start = p.empty[levels]

    else:  # wrong_key
        if parsed.collision_key is None or parsed.collision_value is None or parsed.value is not None:
            return Verdict(Decision.REJECT, Reason.KIND_MISMATCH,
                           "wrong_key 证明缺 collision_* 或携带 value", key_fingerprint=kfp)
        ck = parsed.collision_key
        if len(ck) != p.key_len or ck == parsed.key:
            return Verdict(
                Decision.REJECT, Reason.KEY_BINDING_MISMATCH,
                "碰撞叶键宽错误或与被证键相同", key_fingerprint=kfp,
            )
        # 路径前缀绑定：碰撞叶必须沿被证键的前 levels 个分支位可达
        if any(bit_at(ck, d) != bit_at(parsed.key, d) for d in range(levels)):
            return Verdict(
                Decision.REJECT, Reason.KEY_BINDING_MISMATCH,
                f"碰撞叶不满足前 {levels} 层路径前缀绑定",
                key_fingerprint=kfp, claimed_root=parsed.root.hex(),
                detail={"collision_key": key_fingerprint(ck)},
            )
        start = leaf_digest(ck, parsed.collision_value)

    recomputed = _expand(start, parsed.key, levels, parsed.siblings)
    if recomputed != parsed.root:
        return Verdict(
            Decision.REJECT, Reason.ROOT_MISMATCH,
            "压缩路径展开重算根与证据绑定根不一致",
            key_fingerprint=kfp, claimed_root=parsed.root.hex(),
            recomputed_root=recomputed.hex(),
            detail={"levels": levels, "end": parsed.end},
        )

    reason = (
        Reason.MEMBERSHIP_VERIFIED if parsed.end == END_LEAF
        else Reason.NON_MEMBERSHIP_VERIFIED
    )
    return Verdict(
        Decision.ACCEPT, reason,
        "成员证明核验通过" if parsed.end == END_LEAF else "非成员证明核验通过",
        key_fingerprint=kfp, claimed_root=parsed.root.hex(),
        recomputed_root=recomputed.hex(),
        detail={"levels": levels, "end": parsed.end},
    )
