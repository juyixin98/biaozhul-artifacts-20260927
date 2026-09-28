"""极简助记符汇编器（教学辅助，**不属于共识核心**）。

语法（每行一条，``#`` 之后为注释）::

    PUSH8 5
    PUSH8 3
    ADD
    SSTORE          # 栈顶 key，次顶 value
    CALL            # 紧接的 '{' 与 '}' 之间为内联子字节码
    {
        PUSH1 1
        PUSH1 2
        ADD
        POP
    }
    STOP

支持 ``PUSH1 0..255``、``PUSH8`` 十进制（可负）64 位有符号整数。
内联块可以嵌套；汇编器按词法层级生成 ``[CALL][uleb长度][子字节码]``。
"""
from __future__ import annotations

from .opcodes import Op

_OPEN = "{"
_CLOSE = "}"


class _AssembleError(ValueError):
    pass


def assemble(text: str) -> bytes:
    # 扁平词法流：每个元素 (token, lineno)
    stream: list[tuple[str, int]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        for part in line.split():
            stream.append((part, lineno))
    code, pos = _parse_block(stream, 0, top=True)
    if pos != len(stream):
        tok, lineno = stream[pos]
        raise _AssembleError(f"汇编错误（第 {lineno} 行）：多余的 {tok!r}")
    return bytes(code)


def _parse_block(stream: list[tuple[str, int]], pos: int, top: bool = False) -> tuple[list[int], int]:
    out: list[int] = []
    while pos < len(stream):
        token, lineno = stream[pos]
        if token == _CLOSE:
            if top:
                raise _AssembleError(f"汇编错误（第 {lineno} 行）：多余的 '}}'")
            return out, pos
        pos += 1
        if token == _OPEN:
            raise _AssembleError(
                f"汇编错误（第 {lineno} 行）：'{{' 只能紧跟 CALL 出现"
            )
        try:
            op = Op[token.upper()]
        except KeyError as exc:
            raise _AssembleError(f"汇编错误（第 {lineno} 行）：未知助记符 {token!r}") from exc
        out.append(int(op))

        if op == Op.PUSH1:
            value, pos = _take_int(stream, pos, lineno, token)
            if not (0 <= value <= 255):
                raise _AssembleError(f"汇编错误（第 {lineno} 行）：PUSH1 立即数须在 0..255")
            out.append(value)
        elif op == Op.PUSH8:
            value, pos = _take_int(stream, pos, lineno, token)
            if not (-(1 << 63) <= value < (1 << 64)):
                raise _AssembleError(f"汇编错误（第 {lineno} 行）：PUSH8 立即数超出 64 位")
            out.extend(value.to_bytes(8, "big", signed=value < 0))
        elif op == Op.CALL:
            child, pos = _parse_inline_block(stream, pos, lineno)
            out.extend(_emit_uleb128(len(child)))
            out.extend(child)
    if not top:
        # 被 CALL 调用的块必须由 '}' 结束
        raise _AssembleError("汇编错误：内联块缺少 '}'")
    return out, pos


def _parse_inline_block(stream: list[tuple[str, int]], pos: int, call_lineno: int) -> tuple[list[int], int]:
    if pos >= len(stream) or stream[pos][0] != _OPEN:
        raise _AssembleError(f"汇编错误（第 {call_lineno} 行）：CALL 后缺少 '{{'")
    pos += 1  # 消费 '{'
    child: list[int] = []
    while pos < len(stream) and stream[pos][0] != _CLOSE:
        child_out, pos = _parse_block_from(stream, pos)
        child.extend(child_out)
    if pos >= len(stream):
        raise _AssembleError(f"汇编错误（第 {call_lineno} 行）：CALL 内联块缺少 '}}'")
    if not child:
        raise _AssembleError(f"汇编错误（第 {stream[pos][1]} 行）：CALL 内联块为空")
    return child, pos + 1


def _parse_block_from(stream: list[tuple[str, int]], pos: int) -> tuple[list[int], int]:
    """解析内联块内的一条语句（可能是嵌套 CALL），返回字节与新位置。"""
    token, lineno = stream[pos]
    if token in (_OPEN, _CLOSE):
        raise _AssembleError(f"汇编错误（第 {lineno} 行）：意外的 {token!r}")
    pos += 1
    try:
        op = Op[token.upper()]
    except KeyError as exc:
        raise _AssembleError(f"汇编错误（第 {lineno} 行）：未知助记符 {token!r}") from exc
    code = [int(op)]
    if op == Op.PUSH1:
        value, pos = _take_int(stream, pos, lineno, token)
        if not (0 <= value <= 255):
            raise _AssembleError(f"汇编错误（第 {lineno} 行）：PUSH1 立即数须在 0..255")
        code.append(value)
    elif op == Op.PUSH8:
        value, pos = _take_int(stream, pos, lineno, token)
        if not (-(1 << 63) <= value < (1 << 64)):
            raise _AssembleError(f"汇编错误（第 {lineno} 行）：PUSH8 立即数超出 64 位")
        code.extend(value.to_bytes(8, "big", signed=value < 0))
    elif op == Op.CALL:
        child, pos = _parse_inline_block(stream, pos, lineno)
        code.extend(_emit_uleb128(len(child)))
        code.extend(child)
    return code, pos


def _take_int(stream: list[tuple[str, int]], pos: int, lineno: int, mnemonic: str) -> tuple[int, int]:
    if pos >= len(stream):
        raise _AssembleError(f"汇编错误（第 {lineno} 行）：{mnemonic} 缺少整数参数")
    token, arg_lineno = stream[pos]
    try:
        value = int(token, 0)
    except ValueError as exc:
        raise _AssembleError(f"汇编错误（第 {arg_lineno} 行）：非法整数 {token!r}") from exc
    return value, pos + 1


def _emit_uleb128(value: int) -> list[int]:
    out: list[int] = []
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return out


def disassemble(code: bytes) -> str:
    """反汇编为助记符文本（主要供调试 / 轨迹展示）。"""
    from .opcodes import IMMEDIATES, read_uleb128

    lines: list[str] = []
    pos = 0
    while pos < len(code):
        start = pos
        op = code[pos]
        pos += 1
        if op not in Op._value2member_map_:
            lines.append(f"{start:04x}: ??? 0x{op:02x}")
            continue
        name = Op(op).name
        if op == Op.PUSH1:
            lines.append(f"{start:04x}: PUSH1 {code[pos]}")
            pos += 1
        elif op == Op.PUSH8:
            value = int.from_bytes(code[pos : pos + 8], "big", signed=True)
            lines.append(f"{start:04x}: PUSH8 {value}")
            pos += 8
        elif op == Op.CALL:
            length, data_pos = read_uleb128(code, pos)
            lines.append(f"{start:04x}: CALL ({length} 字节内联)")
            pos = data_pos + length
        else:
            lines.append(f"{start:04x}: {name}")
    return "\n".join(lines)
