"""ABI 编码（严格规范编码，经典 head/tail 两遍模型）。

不变量（与 decoder.py 共享，已经 eth_abi 预言机逐字节验证）：

1. 容器编码 = 静态头(head) + 动态尾(tail)。
2. 静态成员按序占据头部；动态成员在头部占一个 32 字节偏移槽。
3. 偏移**始终相对所在容器自己头部的第一字节**；首个动态块的偏移
   恰好等于本容器静态头总长度。动态嵌套因此绝不使用“绝对文件偏移”。
4. 动态成员块的首字语义：
   - bytes/string：长度字，数据紧随其后；
   - 动态数组 T[]：元素个数字，随后是元素自己的 head/tail，
     元素偏移基准在个数字之后；
   - 动态元组：直接就是它自己的静态头（无额外前缀字）。
"""

from __future__ import annotations

from .errors import (
    ABIValueError,
    AllocationLimitError,
    DepthLimitError,
    LengthMismatchError,
)
from .types import (
    WORD,
    MAX_ARRAY_ELEMENTS,
    MAX_BYTES_LENGTH,
    MAX_DECODE_BYTES,
    MAX_DEPTH,
    AbiType,
    ArrayType,
    ElementaryType,
    TupleType,
)


def _encode_int(t: ElementaryType, value) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ABIValueError(f"{t.name} 需要 int，得到 {type(value).__name__}")
    bits = t.bits
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if t.signed else (0, (1 << bits) - 1)
    if not (lo <= value <= hi):
        raise ABIValueError(f"值 {value} 超出 {t.name} 范围 [{lo}, {hi}]")
    # 带符号补码 → 32 字节，天然包含符号扩展
    return value.to_bytes(WORD, byteorder="big", signed=t.signed)


def _encode_bytes_n(t: ElementaryType, value) -> bytes:
    if not isinstance(value, (bytes, bytearray)):
        raise ABIValueError(f"{t.name} 需要 bytes，得到 {type(value).__name__}")
    if len(value) != t.bits:
        raise LengthMismatchError(f"{t.name} 需要恰好 {t.bits} 字节，得到 {len(value)}")
    return bytes(value) + b"\x00" * (WORD - t.bits)


def _encode_byte_like(raw: bytes) -> bytes:
    """bytes/string 动态块：长度字 + 数据 + 右填充至字边界。"""
    if len(raw) > MAX_BYTES_LENGTH:
        raise AllocationLimitError(f"字节串长度 {len(raw)} 超过硬上限 {MAX_BYTES_LENGTH}")
    pad = (-len(raw)) % WORD
    return len(raw).to_bytes(WORD, "big") + raw + b"\x00" * pad


def _as_sequence(value, t: AbiType) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise ABIValueError(f"{t.name} 需要 list/tuple，得到 {type(value).__name__}")
    return tuple(value)


def _encode_head_tail(t: AbiType, value, depth: int) -> tuple[bytes, bytes]:
    """编码单个成员为 (head_part, tail_part)。

    静态类型：tail=b""，head 为定长编码（占 t.static_size）。
    动态类型：head=b""（调用方在偏移槽补 32 字节占位），tail 为成员块。
    """
    if depth > MAX_DEPTH:
        raise DepthLimitError(f"嵌套深度超过 {MAX_DEPTH}")

    if isinstance(t, ElementaryType):
        if t.kind in ("uint", "int"):
            return _encode_int(t, value), b""
        if t.kind == "bytesN":
            return _encode_bytes_n(t, value), b""
        if t.kind == "bytes":
            if not isinstance(value, (bytes, bytearray)):
                raise ABIValueError(f"bytes 需要 bytes，得到 {type(value).__name__}")
            return b"", _encode_byte_like(bytes(value))
        if t.kind == "string":
            if not isinstance(value, str):
                raise ABIValueError(f"string 需要 str，得到 {type(value).__name__}")
            try:
                return b"", _encode_byte_like(value.encode("utf-8"))
            except UnicodeEncodeError as e:
                raise ABIValueError(f"string 不是合法 UTF-8: {e}") from e
        raise ABIValueError(f"不可编码的基元类型 {t.name}（可能仅选择器模式支持）")

    if isinstance(t, TupleType):
        seq = _as_sequence(value, t)
        if len(seq) != len(t.components):
            raise LengthMismatchError(f"需要 {len(t.components)} 个值，得到 {len(seq)}")
        head, tail = _members(t.components, seq, depth)
        if t.is_dynamic:
            # 动态元组作为父容器的动态成员：完整块 = 自己的头 + 自己的尾，
            # 块首字节即自己头第一字节（父偏移指向这里）。
            return b"", head + tail
        return head, tail

    if isinstance(t, ArrayType):
        seq = _as_sequence(value, t)
        if t.length is None:
            if len(seq) > MAX_ARRAY_ELEMENTS:
                raise AllocationLimitError(
                    f"动态数组元素数 {len(seq)} 超过硬上限 {MAX_ARRAY_ELEMENTS}"
                )
            head, tail = _members((t.element,) * len(seq), seq, depth)
            # 块首是个数字，元素自己的头紧随其后（元素偏移相对该头）。
            return b"", len(seq).to_bytes(WORD, "big") + head + tail
        if len(seq) != t.length:
            raise LengthMismatchError(f"{t.name} 需要 {t.length} 个元素，得到 {len(seq)}")
        head, tail = _members((t.element,) * t.length, seq, depth)
        if t.element.is_dynamic:
            # 元素动态的定长数组整体是动态成员：完整块 = 头 + 尾。
            return b"", head + tail
        return head, tail

    raise ABIValueError(f"未知类型 {t!r}")


def _members(components: tuple[AbiType, ...], values: tuple, depth: int) -> tuple[bytes, bytes]:
    """对一组容器成员做两遍 head/tail 编码。

    返回 (本容器的静态头, 本容器的动态尾)。成员内部偏移始终相对
    “本容器自己头部第一字节”，首个动态成员的偏移 = 本容器头总长。
    """
    parts = [_encode_head_tail(ct, cv, depth + 1) for ct, cv in zip(components, values)]

    def slot_size(ct: AbiType) -> int:
        # 动态成员在本容器头部固定占一个 32 字节偏移槽；
        # 静态成员占其自身静态大小（可能是多个字的静态元组/数组）。
        return WORD if ct.is_dynamic else ct.static_size

    total_head = sum(slot_size(ct) for ct in components)

    out_head = bytearray()
    out_tail = bytearray()
    cursor = total_head  # 相对本容器自己头部第一字节
    for ct, (h, tl) in zip(components, parts):
        if not ct.is_dynamic:
            out_head += h
        else:
            out_head += cursor.to_bytes(WORD, "big")
            out_tail += tl  # tl 已是该动态成员的完整块（见 _encode_head_tail）
            cursor += len(tl)

    blob = bytes(out_head) + bytes(out_tail)
    if len(blob) > MAX_DECODE_BYTES:
        raise AllocationLimitError("容器编码结果超过硬上限")
    return bytes(out_head), bytes(out_tail)


def encode(types, values) -> bytes:
    """按给定类型序列编码顶层值序列（等价于编码一个匿名元组）。"""
    from .types import parse_type

    if not isinstance(types, (list, tuple)):
        raise ABIValueError("types 必须是 list/tuple")
    parsed = tuple(parse_type(t) for t in types)
    values = tuple(values)
    if len(values) != len(parsed):
        raise LengthMismatchError(f"需要 {len(parsed)} 个值，得到 {len(values)}")
    head, tail = _members(parsed, values, depth=0)
    return head + tail
