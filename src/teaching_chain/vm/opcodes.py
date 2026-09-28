"""操作码、助记符表与字节码静态校验。"""
from __future__ import annotations

from enum import IntEnum
from typing import NamedTuple

from . import gas


class Op(IntEnum):
    STOP = 0x00
    ADD = 0x01
    SUB = 0x02
    MUL = 0x03
    DIV = 0x04
    MOD = 0x05
    ADDMOD = 0x06
    MULMOD = 0x07
    LT = 0x10
    GT = 0x11
    EQ = 0x12
    ISZERO = 0x13
    PUSH1 = 0x20   # 后跟 1 字节，零扩展为非负整数 (0..255)
    PUSH8 = 0x21   # 后跟 8 字节，大端有符号 64 位
    POP = 0x30
    DUP1 = 0x31
    SWAP1 = 0x32
    MLOAD = 0x40   # 栈顶：offset -> mem[offset:offset+32] 解释为 32 字节大端有符号整数
    MSTORE = 0x41  # 栈顶：offset，次顶：value -> 将 value 以 32 字节大端有符号写入
    SLOAD = 0x50   # 栈顶：key -> storage[key]（缺失为 0）
    SSTORE = 0x51  # 栈顶：key，次顶：value -> storage[key] = value
    CALL = 0x60    # 立即数 ULEB128 长度 N，后跟 N 字节内联子字节码
    REVERT = 0xFF


# 各操作码的固定（非内存）费用
OP_FEE: dict[int, int] = {
    Op.STOP: gas.G_ZERO,
    Op.ADD: gas.G_VERY_LOW,
    Op.SUB: gas.G_VERY_LOW,
    Op.MUL: gas.G_VERY_LOW,
    Op.DIV: gas.G_LOW,
    Op.MOD: gas.G_LOW,
    Op.ADDMOD: gas.G_LOW,
    Op.MULMOD: gas.G_LOW,
    Op.LT: gas.G_VERY_LOW,
    Op.GT: gas.G_VERY_LOW,
    Op.EQ: gas.G_VERY_LOW,
    Op.ISZERO: gas.G_VERY_LOW,
    Op.PUSH1: gas.G_VERY_LOW,
    Op.PUSH8: gas.G_VERY_LOW,
    Op.POP: gas.G_BASE,
    Op.DUP1: gas.G_VERY_LOW,
    Op.SWAP1: gas.G_VERY_LOW,
    Op.MLOAD: gas.G_VERY_LOW,
    Op.MSTORE: gas.G_VERY_LOW,
    Op.SLOAD: gas.G_LOW,
    Op.SSTORE: gas.G_SSTORE,
    Op.CALL: gas.G_CALL_BASE,
    Op.REVERT: gas.G_ZERO,
}

# 弹出 / 压入栈的个数（用于静态与运行时检查；CALL 不使用栈参数）
OP_STACK_EFFECT: dict[int, tuple[int, int]] = {
    Op.STOP: (0, 0),
    Op.ADD: (2, 1),
    Op.SUB: (2, 1),
    Op.MUL: (2, 1),
    Op.DIV: (2, 1),
    Op.MOD: (2, 1),
    Op.ADDMOD: (3, 1),
    Op.MULMOD: (3, 1),
    Op.LT: (2, 1),
    Op.GT: (2, 1),
    Op.EQ: (2, 1),
    Op.ISZERO: (1, 1),
    Op.PUSH1: (0, 1),
    Op.PUSH8: (0, 1),
    Op.POP: (1, 0),
    Op.DUP1: (1, 2),
    Op.SWAP1: (2, 2),
    Op.MLOAD: (1, 1),
    Op.MSTORE: (2, 0),
    Op.SLOAD: (1, 1),
    Op.SSTORE: (2, 0),
    Op.CALL: (0, 0),
    Op.REVERT: (0, 0),
}


class InvalidBytecode(ValueError):
    """字节码静态校验失败（未知操作码 / 立即数截断）。"""


class Immediate(NamedTuple):
    """立即数描述。"""

    size: int | None = None     # 固定立即数大小（字节）
    uleb: bool = False          # 是否为 ULEB128 长度前缀


IMMEDIATES: dict[int, Immediate] = {
    Op.PUSH1: Immediate(size=1),
    Op.PUSH8: Immediate(size=8),
    Op.CALL: Immediate(uleb=True),
}


def read_uleb128(code: bytes, pos: int) -> tuple[int, int]:
    """从 pos 读取 ULEB128，返回 (值, 长度前缀结束位置)。"""
    result = 0
    shift = 0
    while True:
        if pos >= len(code):
            raise InvalidBytecode("ULEB128 立即数被截断")
        b = code[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise InvalidBytecode("ULEB128 立即数过长")


def validate_bytecode(code: bytes) -> None:
    """静态走查整段字节码：操作码合法、立即数完整。

    CALL 的内联子字节码也被递归校验（其内部可能再含 CALL）。
    """
    _validate_range(code, 0, len(code), depth=0)


def _validate_range(code: bytes, start: int, end: int, depth: int) -> None:
    if depth > 32:
        raise InvalidBytecode("CALL 嵌套深度超过 32")
    pos = start
    while pos < end:
        op = code[pos]
        if op not in OP_FEE:
            raise InvalidBytecode(f"未知操作码 0x{op:02x}（pc={pos}）")
        pos += 1
        imm = IMMEDIATES.get(op)
        if imm is None:
            continue
        if imm.uleb:
            length, data_pos = read_uleb128(code, pos)
            if length > 10_000:
                raise InvalidBytecode(f"CALL 内联字节码过长（{length}，上限 10000）")
            if data_pos + length > end:
                raise InvalidBytecode("CALL 内联字节码超出父字节码范围")
            _validate_range(code, data_pos, data_pos + length, depth + 1)
            pos = data_pos + length
        else:
            assert imm.size is not None
            if pos + imm.size > end:
                raise InvalidBytecode(f"操作码 0x{op:02x} 的立即数被截断")
            pos += imm.size
