"""脚本/栈元素编解码与最小 ScriptNum。

脚本 = 指令序列：
  OP_0                无操作数
  0x01..0x4b + N 字节 直接压入
  PUSHDATA1/2/4       长度前缀压入
  其它支持的操作码     无操作数

栈元素统一为 bytes（最长 255 字节，运行前/压入前检查）。
ScriptNum 为 Bitcoin 风格“最小有符号幅值、小端序”，宽度上限 4 字节。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import opcodes as O
from .errors import FailCode, VmFailure


@dataclass(frozen=True)
class Instruction:
    op: int                 # 操作码（直接压入时是 0x01..0x4b）
    data: bytes | None      # 压入数据；非压入指令为 None
    offset: int             # 指令在原脚本中的起始偏移

    @property
    def is_push(self) -> bool:
        return O.is_push_op(self.op)

    @property
    def name(self) -> str:
        return O.op_name(self.op)


def parse(script: bytes, *, max_script_bytes: int | None = None) -> list[Instruction]:
    """解码脚本。结构问题 → SCRIPT_MALFORMED；未知码 → UNKNOWN_OPCODE。"""
    if not isinstance(script, (bytes, bytearray, memoryview)):
        raise VmFailure(FailCode.SCRIPT_MALFORMED, "脚本必须是字节序列")
    script = bytes(script)
    if max_script_bytes is not None and len(script) > max_script_bytes:
        raise VmFailure(
            FailCode.SCRIPT_TOO_LARGE,
            f"脚本 {len(script)} 字节超过上限 {max_script_bytes}",
        )

    out: list[Instruction] = []
    i = 0
    n = len(script)
    while i < n:
        start = i
        code = script[i]
        i += 1

        if O.PUSH_DIRECT_MIN <= code <= O.PUSH_DIRECT_MAX:
            if i + code > n:
                raise VmFailure(
                    FailCode.SCRIPT_MALFORMED,
                    f"偏移 {start}: PUSHBYTES_{code} 数据被截断",
                )
            out.append(Instruction(code, script[i:i + code], start))
            i += code
            continue

        if code in (O.OP_PUSHDATA1, O.OP_PUSHDATA2, O.OP_PUSHDATA4):
            width = {O.OP_PUSHDATA1: 1, O.OP_PUSHDATA2: 2, O.OP_PUSHDATA4: 4}[code]
            if i + width > n:
                raise VmFailure(
                    FailCode.SCRIPT_MALFORMED,
                    f"偏移 {start}: {O.op_name(code)} 长度前缀被截断",
                )
            length = int.from_bytes(script[i:i + width], "little")
            i += width
            if i + length > n:
                raise VmFailure(
                    FailCode.SCRIPT_MALFORMED,
                    f"偏移 {start}: {O.op_name(code)} 数据被截断（需要 {length} 字节）",
                )
            out.append(Instruction(code, script[i:i + length], start))
            i += length
            continue

        if not O.is_supported(code):
            raise VmFailure(
                FailCode.UNKNOWN_OPCODE,
                f"偏移 {start}: 不支持的字节 0x{code:02x}",
            )
        out.append(Instruction(code, None, start))

    return out


def assert_push_only(script: bytes, *, max_script_bytes: int) -> None:
    """解锁脚本只允许压入操作（在完整解码后再检查）。"""
    for ins in parse(script, max_script_bytes=max_script_bytes):
        if not ins.is_push:
            raise VmFailure(
                FailCode.PUSH_ONLY_VIOLATION,
                f"解锁脚本偏移 {ins.offset} 含非压入操作 {ins.name}",
            )


def encode_push(data: bytes, *, max_element_bytes: int = 255) -> bytes:
    """把一个元素编码为最小长度的压入指令序列。"""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise VmFailure(FailCode.SCRIPT_MALFORMED, "压入元素必须是字节序列")
    data = bytes(data)
    if len(data) > max_element_bytes:
        raise VmFailure(
            FailCode.ELEMENT_TOO_LARGE,
            f"元素 {len(data)} 字节超过上限 {max_element_bytes}",
        )
    n = len(data)
    if n == 0:
        return bytes([O.Op.OP_0])
    if n <= O.PUSH_DIRECT_MAX:
        return bytes([n]) + data
    if n <= 0xFF:
        return bytes([O.OP_PUSHDATA1, n]) + data
    if n <= 0xFFFF:
        return bytes([O.OP_PUSHDATA2]) + n.to_bytes(2, "little") + data
    return bytes([O.OP_PUSHDATA4]) + n.to_bytes(4, "little") + data


def assemble(parts) -> bytes:
    """组装脚本：parts 元素为 int（操作码/小整数）或 bytes（压入数据）。"""
    out = bytearray()
    for p in parts:
        if isinstance(p, int):
            if p == 0:
                out.append(O.Op.OP_0)
            elif 1 <= p <= 16:
                out.append(O.OP_1 + p - 1)
            elif p in set(O.Op):
                out.append(p)
            else:
                raise VmFailure(FailCode.SCRIPT_MALFORMED, f"无法汇编操作码 {p}")
        elif isinstance(p, (bytes, bytearray)):
            out += encode_push(bytes(p))
        else:
            raise VmFailure(FailCode.SCRIPT_MALFORMED, f"无法汇编片段 {p!r}")
    return bytes(out)


def disassemble(script: bytes) -> str:
    """人类可读反汇编（仅用于日志/调试）。"""
    items = []
    for ins in parse(script):
        if ins.is_push:
            items.append(f"{ins.name} 0x{ins.data.hex()}" if ins.data else ins.name)
        else:
            items.append(ins.name)
    return " ".join(items)


# ----------------------------- ScriptNum -----------------------------

def encode_scriptnum(value: int, *, max_bytes: int = 4) -> bytes:
    if value == 0:
        return b""
    neg = value < 0
    v = -value if neg else value
    out = bytearray()
    while v:
        out.append(v & 0xFF)
        v >>= 8
    if out[-1] & 0x80:
        out.append(0x80 if neg else 0x00)
    elif neg:
        out[-1] |= 0x80
    if len(out) > max_bytes:
        raise VmFailure(
            FailCode.INT_OVERFLOW,
            f"ScriptNum 编码需要 {len(out)} 字节，超过上限 {max_bytes}",
        )
    return bytes(out)


def decode_scriptnum(data: bytes, *, max_bytes: int = 4) -> int:
    """按最小有符号幅值小端序解码；非最小宽度但不超过 max_bytes 也接受。"""
    if len(data) > max_bytes:
        raise VmFailure(
            FailCode.INT_OVERFLOW,
            f"ScriptNum 元素 {len(data)} 字节，超过上限 {max_bytes}",
        )
    if not data:
        return 0
    result = int.from_bytes(data, "little")
    if data[-1] & 0x80:
        # 最高位是符号位：去掉它
        result &= ~(0x80 << (8 * (len(data) - 1)))
        return -result
    return result


def cast_bool(data: bytes) -> bool:
    """栈真值：非零即真；字节序列末尾的 0x80 不构成正值。"""
    if not data:
        return False
    for i, b in enumerate(data):
        if b != 0:
            if i == len(data) - 1 and b == 0x80:
                return False
            return True
    return False
