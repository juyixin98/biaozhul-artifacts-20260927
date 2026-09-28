"""唯一支持的操作码白名单。

编号沿用 Bitcoin 历史编号仅为便于阅读；本项目不是 Bitcoin 实现，语义以下列常量
和 stackvm.vm 的实现为准。白名单之外的任何字节在解码期即判 UNKNOWN_OPCODE。
"""
from __future__ import annotations

from enum import IntEnum

# 直接压入：0x01..0x4b（后随 N 个数据字节）
PUSH_DIRECT_MIN = 0x01
PUSH_DIRECT_MAX = 0x4B
OP_PUSHDATA1 = 0x4C
OP_PUSHDATA2 = 0x4D
OP_PUSHDATA4 = 0x4E

# OP_1NEGATE 不支持；OP_1..OP_16
OP_1 = 0x51
OP_16 = 0x60


class Op(IntEnum):
    OP_0 = 0x00
    OP_PUSHDATA1 = 0x4C
    OP_PUSHDATA2 = 0x4D
    OP_PUSHDATA4 = 0x4E

    OP_NOP = 0x61
    OP_IF = 0x63
    OP_NOTIF = 0x64
    OP_ELSE = 0x67
    OP_ENDIF = 0x68
    OP_VERIFY = 0x69
    OP_RETURN = 0x6A

    OP_TOALTSTACK = 0x6B
    OP_FROMALTSTACK = 0x6C

    OP_DROP = 0x75
    OP_DUP = 0x76
    OP_SWAP = 0x7C
    OP_SIZE = 0x82

    OP_EQUAL = 0x87
    OP_EQUALVERIFY = 0x88

    OP_ADD = 0x93

    OP_RIPEMD160 = 0xA6
    OP_SHA1 = 0xA7
    OP_SHA256 = 0xA8
    OP_HASH160 = 0xA9
    OP_HASH256 = 0xAA

    OP_CHECKSIG = 0xAB
    OP_CHECKSIGVERIFY = 0xAC
    OP_CHECKMULTISIG = 0xAE
    OP_CHECKMULTISIGVERIFY = 0xAF


def op_name(code: int) -> str:
    if PUSH_DIRECT_MIN <= code <= PUSH_DIRECT_MAX:
        return f"PUSHBYTES_{code}"
    if OP_1 <= code <= OP_16:
        return f"OP_{code - OP_1 + 1}"
    try:
        return Op(code).name
    except ValueError:
        return f"UNKNOWN_0x{code:02x}"


def is_supported(code: int) -> bool:
    if PUSH_DIRECT_MIN <= code <= PUSH_DIRECT_MAX:
        return True
    if OP_1 <= code <= OP_16:
        return True
    return code in set(Op)


def is_push_op(code: int) -> bool:
    """仅压入语义的操作（解锁脚本白名单）。"""
    if code == Op.OP_0:
        return True
    if PUSH_DIRECT_MIN <= code <= PUSH_DIRECT_MAX:
        return True
    if code in (OP_PUSHDATA1, OP_PUSHDATA2, OP_PUSHDATA4):
        return True
    if OP_1 <= code <= OP_16:
        return True
    return False


def small_int_op(value: int) -> int | None:
    if value == 0:
        return Op.OP_0
    if 1 <= value <= 16:
        return OP_1 + value - 1
    return None
