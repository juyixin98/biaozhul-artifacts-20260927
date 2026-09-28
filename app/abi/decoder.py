"""严格 ABI 解码（经典 head/tail 模型，与 encoder.py 一一对应）。

安全设计要点：
1. 所有读取都经过带边界检查的 ``_read_word``，永不按外部偏移做裸切片；
2. 动态偏移一律解释为**相对所在容器自己头部起点**：
   - 元组：元组头第一字节；
   - 动态数组：长度字之后的第一字节（长度字位于偏移 -32）；
   绝不能当成整个 blob 的绝对文件偏移；
3. 动态子块按偏移排序后必须**首尾相接铺满尾部**——既拒绝重叠，
   也拒绝未声明间隙（non-canonical layout）；
4. 所有 length/offset 先与硬上限比较，再做任何乘法/分配；
5. 整数与字节尾部要求规范填充，否则 NonCanonicalPaddingError；
6. 顶层要求恰好消费全部输入（比 eth_abi 更严格，拒绝尾随垃圾）。
"""

from __future__ import annotations

from .errors import (
    ABIDecodeError,
    AllocationLimitError,
    DepthLimitError,
    NonCanonicalLayoutError,
    NonCanonicalPaddingError,
    OffsetOutOfBoundsError,
    OverlapError,
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


def _read_word(data: bytes, pos: int, *, what: str) -> bytes:
    end = pos + WORD
    if pos < 0 or end > len(data):
        raise OffsetOutOfBoundsError(
            f"{what}: 读取 32 字节越界 [pos={pos}, blob_len={len(data)}]"
        )
    return data[pos:end]


def _read_uint_word(data: bytes, pos: int, *, what: str) -> int:
    return int.from_bytes(_read_word(data, pos, what=what), "big", signed=False)


def decode(types, data: bytes):
    """解码顶层值序列。返回 tuple；要求恰好消费 data 全部字节。"""
    from .types import parse_type

    if not isinstance(data, (bytes, bytearray)):
        raise ABIDecodeError(f"输入必须是 bytes，得到 {type(data).__name__}")
    data = bytes(data)
    if len(data) > MAX_DECODE_BYTES:
        raise AllocationLimitError(f"输入 {len(data)} 字节超过硬上限 {MAX_DECODE_BYTES}")

    parsed = tuple(parse_type(t) for t in types)
    if not parsed:
        if data:
            raise NonCanonicalLayoutError("空类型序列但输入非空")
        return ()

    values, end = _members(data, 0, len(data), parsed, 0)
    if end != len(data):
        raise NonCanonicalLayoutError(
            f"顶层解码结束于 {end}，但输入长度为 {len(data)}：存在尾随字节（拒绝）"
        )
    return tuple(values)


# ---------------------------------------------------------------------------


def _slot_size(t: AbiType) -> int:
    return WORD if t.is_dynamic else t.static_size


def _members(data: bytes, head_start: int, limit: int,
             components: tuple[AbiType, ...], depth: int):
    """解码容器的 head + tail。

    head_start —— 本容器头部第一字节（动态成员偏移的解释基准）；
    limit      —— 本容器独占区间末端（绝对索引，排他）。
    返回 (值列表, head+tail 结束绝对索引)。
    """
    if depth > MAX_DEPTH:
        raise DepthLimitError(f"嵌套深度超过 {MAX_DEPTH}")

    total_head = sum(_slot_size(c) for c in components)
    head_end = head_start + total_head
    if head_end > limit:
        raise OffsetOutOfBoundsError(
            f"容器静态头 {total_head} 字节超出区间 [head_start={head_start}, limit={limit}]"
        )

    # 第一遍：静态成员就地解码；收集动态成员的 (相对偏移, idx, type, 槽位)
    values: list = [None] * len(components)
    dynamic: list[tuple[int, int, AbiType]] = []
    cursor = head_start
    for idx, ct in enumerate(components):
        if ct.is_dynamic:
            rel = _read_uint_word(data, cursor, what=f"成员[{idx}] {ct.name} 偏移")
            dynamic.append((rel, idx, ct))
        else:
            values[idx] = _static(data, cursor, ct, depth)
        cursor += _slot_size(ct)

    # 第二遍：动态成员按偏移排序，必须从 head_end 起首尾相接
    ordered = sorted(dynamic, key=lambda e: e[0])
    expected_rel = total_head
    consumed_end = head_end  # 无动态成员时，容器只消费静态头
    for pos, (rel, idx, ct) in enumerate(ordered):
        block_start = head_start + rel
        child_limit = head_start + ordered[pos + 1][0] if pos + 1 < len(ordered) else limit
        # 1) 先判越界：块首已落在容器独占区间之外（指向外部绝对位置）。
        if block_start > limit:
            raise OffsetOutOfBoundsError(
                f"成员[{idx}] {ct.name} 偏移 {rel} 越过容器末端 "
                f"(block_start={block_start} > limit={limit})"
            )
        # 2) 指入本容器静态头/长度区域 → 重叠
        if rel < total_head:
            raise OverlapError(
                f"成员[{idx}] {ct.name} 偏移 {rel} 指入静态头区（头长 {total_head}）"
            )
        # 3) 与前一动态块重叠（偏移未按序后移）
        if rel < expected_rel:
            raise OverlapError(
                f"成员[{idx}] {ct.name} 偏移 {rel} 与前一动态块重叠（应 >= {expected_rel}）"
            )
        # 4) 与前块之间留出未声明间隙 → 非规范布局
        if rel > expected_rel:
            raise NonCanonicalLayoutError(
                f"成员[{idx}] {ct.name} 偏移 {rel} 与前块间存在 "
                f"{rel - expected_rel} 字节未声明间隙（拒绝非规范布局）"
            )
        values[idx] = _dynamic(data, block_start, child_limit, ct, depth)
        expected_rel = child_limit - head_start
        consumed_end = child_limit

    return values, consumed_end


def _static(data: bytes, pos: int, t: AbiType, depth: int):
    """解码位于 pos、占 t.static_size 的静态类型。"""
    if isinstance(t, ElementaryType):
        return _elementary(data, pos, t)
    if isinstance(t, TupleType):
        vals, _ = _members(data, pos, pos + t.static_size, t.components, depth + 1)
        return tuple(vals)
    if isinstance(t, ArrayType):
        # 仅静态定长数组到达这里
        comps = (t.element,) * (t.length or 0)
        vals, _ = _members(data, pos, pos + t.static_size, comps, depth + 1)
        return vals
    raise ABIDecodeError(f"非静态可解码类型 {t.name}")


def _dynamic(data: bytes, block_start: int, limit: int, t: AbiType, depth: int):
    """解码独占区间 [block_start, limit) 的动态成员。

    block_start 指向成员块第一字节：
      bytes/string → 长度字；动态数组 → 个数字；动态元组 → 自己的头。
    """
    if isinstance(t, ElementaryType):
        if t.kind == "bytes":
            return _bytes_like(data, block_start, limit, as_string=False)
        if t.kind == "string":
            return _bytes_like(data, block_start, limit, as_string=True)
        raise ABIDecodeError(f"意外的动态基元 {t.name}")

    if isinstance(t, TupleType):
        vals, end = _members(data, block_start, limit, t.components, depth + 1)
        if end != limit:
            raise NonCanonicalLayoutError(
                f"动态元组结束于 {end}，声明区间末端 {limit}"
            )
        return tuple(vals)

    if isinstance(t, ArrayType):
        if t.length is None:
            length = _read_uint_word(data, block_start, what="动态数组长度")
            if length > MAX_ARRAY_ELEMENTS:
                raise AllocationLimitError(
                    f"动态数组长度 {length} 超过硬上限 {MAX_ARRAY_ELEMENTS}"
                )
            # 成员偏移基准在个数字之后
            vals, end = _members(
                data, block_start + WORD, limit, (t.element,) * length, depth + 1
            )
        else:
            # 元素动态的定长数组：块首即自己的头
            vals, end = _members(
                data, block_start, limit, (t.element,) * t.length, depth + 1
            )
        if end != limit:
            raise NonCanonicalLayoutError(
                f"动态数组结束于 {end}，声明区间末端 {limit}"
            )
        return vals

    raise ABIDecodeError(f"未知动态类型 {t.name}")


# ---------------------------------------------------------------------------


def _elementary(data: bytes, pos: int, t: ElementaryType):
    word = _read_word(data, pos, what=t.name)
    if t.kind in ("uint", "int"):
        bits = t.bits
        raw = int.from_bytes(word, "big", signed=False)  # 始终先按 256 位无符号读
        if t.signed:
            # 取低 bits 位，按声明位宽做符号扩展
            low = raw & ((1 << bits) - 1)
            value = low - (1 << bits) if low >= 1 << (bits - 1) else low
            # 规范符号扩展：高 (256-bits) 位必须全 1（负数）或全 0（非负）
            sign_fill = ((1 << (256 - bits)) - 1) << bits
            expected_high = sign_fill if value < 0 else 0
            if (raw & sign_fill) != expected_high:
                raise NonCanonicalPaddingError(
                    f"{t.name} 非规范符号扩展：原始字 {word.hex()}"
                )
        else:
            value = raw
            # uint：高 (256-bits) 位必须全 0（同时即值域检查）
            if raw >> bits != 0:
                raise NonCanonicalPaddingError(
                    f"{t.name} 非规范填充/超范围：原始字 {word.hex()}，"
                    f"高 {256 - bits} 位必须全为 0"
                )
        return value
    if t.kind == "bytesN":
        if any(b != 0 for b in word[t.bits :]):
            raise NonCanonicalPaddingError(
                f"{t.name} 尾部 {WORD - t.bits} 字节必须为 0，得到 {word.hex()}"
            )
        return word[: t.bits]
    raise ABIDecodeError(f"不支持解码的基元 {t.name}")


def _bytes_like(data: bytes, block_start: int, limit: int, *, as_string: bool):
    """bytes/string 动态块：长度字 + 数据 + 零填充至字边界。"""
    length = _read_uint_word(data, block_start, what="bytes/string 长度")
    if length > MAX_BYTES_LENGTH:
        raise AllocationLimitError(f"bytes/string 长度 {length} 超过硬上限")
    payload_start = block_start + WORD
    payload_end = payload_start + length
    block_end = payload_end + (-length) % WORD
    if block_end > limit:
        raise OffsetOutOfBoundsError(
            f"bytes/string 声明长度 {length}（含填充 {block_end - block_start} 字节）"
            f"超出独占区间末端 {limit}"
        )
    if block_end < limit:
        raise NonCanonicalLayoutError(
            f"bytes/string 块结束于 {block_end}，未铺满独占区间末端 {limit}"
        )
    if payload_end > len(data):
        raise OffsetOutOfBoundsError("bytes/string 数据越过 blob 末端")
    raw = data[payload_start:payload_end]
    pad = (-length) % WORD
    if pad and any(b != 0 for b in data[payload_end:block_end]):
        raise NonCanonicalPaddingError("bytes/string 尾部填充必须全为 0")
    if as_string:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ABIDecodeError(f"string 负载不是合法 UTF-8: {e}") from e
    return raw
