"""操作码表与程序编码（u64 字、大端）。

程序是一串操作码，PUSH 后跟一个 8 字节大端立即数。程序集见 README「指令集」。
编码/解码是确定性的：同一字节串在所有进程上解码出同一指令序列。
"""

from __future__ import annotations

from enum import IntEnum


class Op(IntEnum):
    STOP = 0x00
    ADD = 0x01
    SUB = 0x02
    MUL = 0x03
    DIV = 0x04
    MOD = 0x05
    LT = 0x10
    GT = 0x11
    EQ = 0x12
    ISZERO = 0x13
    PUSH = 0x20
    POP = 0x21
    DUP = 0x22
    SWAP = 0x23
    JUMPDEST = 0x30
    JUMP = 0x31
    JUMPI = 0x32
    CALLDATALOAD = 0x40
    CALLDATASIZE = 0x41
    MLOAD = 0x50
    MSTORE = 0x51
    MSIZE = 0x52
    SLOAD = 0x60
    SSTORE = 0x61
    CALL = 0x70
    RETURN = 0xF3
    REVERT = 0xFD
    INVALID = 0xFE


# 立即数长度（字节）
IMM_SIZES: dict[int, int] = {
    Op.PUSH: 8,
    Op.DUP: 1,
    Op.SWAP: 1,
}

# 操作码助记符（诊断/反汇编用）
NAMES: dict[int, str] = {op: op.name for op in Op}

# 字节码末尾哨兵；真实程序不可包含该字节
END_MARKER = 0xFF
END_MARKER_BYTES = bytes([END_MARKER])


class Instruction:
    __slots__ = ("op", "imm", "pc")

    def __init__(self, op: int, imm: int | None, pc: int) -> None:
        self.op = op
        self.imm = imm
        self.pc = pc  # 指令首字节在原程序中的偏移


def decode(code: bytes) -> list[Instruction]:
    """线性解码。非法字节、截断的立即数、内嵌结束标记都抛 ValueError。"""
    instrs: list[Instruction] = []
    i = 0
    n = len(code)
    while i < n:
        b = code[i]
        if b == END_MARKER:
            raise ValueError(f"embedded end marker 0x{END_MARKER:02x} at byte {i}")
        try:
            op = Op(b)
        except ValueError:
            raise ValueError(f"invalid opcode 0x{b:02x} at byte {i}") from None
        size = IMM_SIZES.get(b, 0)
        imm: int | None = None
        if size:
            if i + 1 + size > n:
                raise ValueError(f"truncated immediate for {op.name} at byte {i}")
            raw = code[i + 1 : i + 1 + size]
            imm = int.from_bytes(raw, "big", signed=False)
            if op in (Op.DUP, Op.SWAP):
                if not (1 <= imm <= 16):
                    raise ValueError(f"{op.name} index out of range 1..16 at byte {i}: {imm}")
        instrs.append(Instruction(int(op), imm, i))
        i += 1 + size
    return instrs


def assemble(text: str) -> bytes:
    """极简教学汇编器（两趟，支持标号），供示例/测试/CLI 使用。

    语法（每行一条）：

    * ``PUSH 123`` / ``PUSH 0x10`` —— 8 字节大端立即数；
    * ``DUP 1``、``SWAP 2`` —— 1 字节立即数（1..16）；
    * ``loop:`` —— 行首标号，绑定其后指令的**字节偏移**，供 JUMP/JUMPI 引用；
    * ``# 注释``、空行忽略。

    栈操作数顺序与运行时一致：``JUMP`` 弹出的目标可由 ``PUSH loop`` 压入。
    """
    lines: list[tuple[int, str, str]] = []  # (行号, 助记符, 操作数文本)
    labels: dict[str, int] = {}
    offset = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.endswith(":"):
            name = line[:-1].strip()
            if not name or not name.replace("_", "").isalnum():
                raise ValueError(f"line {lineno}: bad label {name!r}")
            labels[name] = offset
            continue
        parts = line.replace(",", " ").split()
        name = parts[0].upper()
        if name not in NAMES.values():
            raise ValueError(f"line {lineno}: unknown mnemonic {name}")
        op = Op[name]
        lines.append((lineno, name, parts[1] if len(parts) > 1 else ""))
        offset += 1 + IMM_SIZES.get(int(op), 0)

    out = bytearray()
    for lineno, name, operand in lines:
        op = Op[name]
        out.append(int(op))
        size = IMM_SIZES.get(int(op), 0)
        if not size:
            continue
        if not operand:
            raise ValueError(f"line {lineno}: {name} requires an immediate")
        if operand in labels:
            value = labels[operand]
        else:
            value = int(operand, 0)
        if op in (Op.DUP, Op.SWAP):
            if not (1 <= value <= 16):
                raise ValueError(f"line {lineno}: {name} index out of range")
            out.append(value)
        else:
            if not (0 <= value <= 2**64 - 1):
                raise ValueError(f"line {lineno}: immediate out of u64")
            out.extend(value.to_bytes(8, "big"))
    return bytes(out)
