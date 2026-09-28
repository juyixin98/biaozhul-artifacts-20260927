"""证明信封（线格式）：把逻辑证明绑定到根、键与深度参数。

信封 schema 固定为 ``smt-proof/v1``。序列化采用规范化 JSON（排序键、无空白），
字段集合封闭——多余/缺失字段一律 ENVELOPE_MALFORMED，使证明不可向旧验证器
偷偷塞入新字段。深度与键宽在信封内随证明一起绑定，验证器若以不同参数核验，
直接 DEPTH_MISMATCH。
"""
from __future__ import annotations

from app.coding.params import TreeParams
from app.coding.serialization import canonical_json
from app.core.proof import (
    END_EMPTY,
    END_LEAF,
    END_WRONG_KEY,
    LogicalProof,
)

PROOF_SCHEMA = "smt-proof/v1"

_TOP_FIELDS = {
    "schema", "key_len", "depth", "key", "root", "end",
    "bitmap", "siblings", "value", "collision_key", "collision_value",
}
_END_VALUES = {END_EMPTY, END_LEAF, END_WRONG_KEY}


class EnvelopeError(ValueError):
    """信封格式错误。"""


def proof_to_envelope(proof: LogicalProof, params: TreeParams) -> dict:
    envelope = {
        "schema": PROOF_SCHEMA,
        "key_len": params.key_len,
        "depth": params.depth,
        "key": proof.key.hex(),
        "root": proof.root.hex(),
        "end": proof.end,
        "bitmap": proof.bitmap.hex(),
        "siblings": [s.hex() for s in proof.siblings],
        "value": None if proof.value is None else proof.value.hex(),
        "collision_key": None if proof.collision_key is None else proof.collision_key.hex(),
        "collision_value": None if proof.collision_value is None else proof.collision_value.hex(),
    }
    return envelope


def proof_from_envelope(envelope: dict) -> tuple[LogicalProof, TreeParams]:
    """严格解析信封。未知字段、类型错误、hex 错误均抛 EnvelopeError。"""
    if not isinstance(envelope, dict):
        raise EnvelopeError("证明信封必须是 JSON 对象")
    extra = set(envelope) - _TOP_FIELDS
    if extra:
        raise EnvelopeError(f"证明信封含未知字段: {sorted(extra)}")
    missing = _TOP_FIELDS - set(envelope)
    if missing:
        raise EnvelopeError(f"证明信封缺字段: {sorted(missing)}")
    if envelope["schema"] != PROOF_SCHEMA:
        raise EnvelopeError(f"不支持的证明 schema: {envelope['schema']!r}")

    try:
        key_len = int(envelope["key_len"])
        depth = int(envelope["depth"])
        params = TreeParams(key_len=key_len, depth=depth)
    except (ValueError, TypeError) as exc:
        raise EnvelopeError(f"树参数非法: {exc}") from exc

    end = envelope["end"]
    if end not in _END_VALUES:
        raise EnvelopeError(f"end 必须是 {sorted(_END_VALUES)}")

    def _hex(field: str, required_len: int | None) -> bytes:
        raw = envelope[field]
        if not isinstance(raw, str):
            raise EnvelopeError(f"{field} 必须是 hex 字符串")
        try:
            data = bytes.fromhex(raw)
        except ValueError as exc:
            raise EnvelopeError(f"{field} 不是合法 hex: {exc}") from exc
        if required_len is not None and len(data) != required_len:
            raise EnvelopeError(f"{field} 长度必须为 {required_len} 字节")
        return data

    try:
        key = _hex("key", key_len)
        root = _hex("root", 32)
        bitmap = _hex("bitmap", (depth + 7) // 8)
        siblings = []
        for i, item in enumerate(envelope["siblings"]):
            if not isinstance(item, str):
                raise EnvelopeError(f"siblings[{i}] 必须是 hex 字符串")
            siblings.append(_hex_str(item, f"siblings[{i}]", 32))

        value = None
        if envelope["value"] is not None:
            value = _hex_optional(envelope["value"], "value")
        collision_key = None
        collision_value = None
        if end == END_WRONG_KEY:
            collision_key = _hex("collision_key", key_len)
            collision_value = _hex_optional(envelope["collision_value"], "collision_value")
        elif envelope["collision_key"] is not None or envelope["collision_value"] is not None:
            raise EnvelopeError("仅 wrong_key 证明允许携带 collision_* 字段")
    except EnvelopeError:
        raise

    proof = LogicalProof(
        key=key, root=root, end=end, bitmap=bitmap, siblings=siblings,
        value=value, collision_key=collision_key, collision_value=collision_value,
    )
    return proof, params


def _hex_str(raw: str, field: str, length: int) -> bytes:
    try:
        data = bytes.fromhex(raw)
    except ValueError as exc:
        raise EnvelopeError(f"{field} 不是合法 hex: {exc}") from exc
    if len(data) != length:
        raise EnvelopeError(f"{field} 长度必须为 {length} 字节")
    return data


def _hex_optional(raw, field: str) -> bytes:
    if not isinstance(raw, str):
        raise EnvelopeError(f"{field} 必须是 hex 字符串")
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise EnvelopeError(f"{field} 不是合法 hex: {exc}") from exc


def canonical_envelope_bytes(envelope: dict) -> bytes:
    """信封的规范字节串（日志指纹/调试用；签名只签版本检查点，不签整信封）。"""
    return canonical_json(envelope)
