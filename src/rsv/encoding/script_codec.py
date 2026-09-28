"""脚本序列化与解析（模块一：编码）。

解析器只做严格解码：
- 0x01..0x4B：后随恰好 N 字节的直接推送；
- PUSHDATA1/2/4：长度前缀必须与剩余字节精确匹配，且要求“最小长度前缀”
  （能直接推送就不允许 PUSHDATA，避免同构异码）；
- 其余字节必须在 opcodes.SUPPORTED 白名单内；
- IF/ELSE/ENDIF 在解析期做配对与深度检查（执行期语义在 vm 中）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .opcodes import (
    DIRECT_PUSH_MAX,
    DIRECT_PUSH_MIN,
    OP_DATA_1,
    OP_DATA_2,
    OP_DATA_4,
    OP_ELSE,
    OP_ENDIF,
    OP_IF,
    OP_NOTIF,
    OP_0,
    SUPPORTED,
)
from ..config import Limits
from ..errors import (
    MALFORMED_PUSH,
    RESERVED_OPCODE,
    SCRIPT_TOO_LARGE,
    UNBALANCED_IF,
    UNKNOWN_OPCODE,
    VerificationFailure,
)


@dataclass(frozen=True)
class Instruction:
    op: int
    data: bytes  # 非推送时为 b""
    pc: int  # 指令序号（不是字节偏移，便于和步预算对齐）
    offset: int  # 原始脚本字节偏移


# 经典栈脚本中保留或在受限环境禁用的字节
_RESERVED_BYTES = frozenset(
    {0x50, 0x62, 0x65, 0x66, 0x7D, 0x7E, 0x7F, 0x80, 0x81, 0x83, 0x84, 0x85, 0x86}
)


def parse_script(raw: bytes, limits: Limits) -> list[Instruction]:
    if len(raw) > limits.max_script_bytes:
        raise VerificationFailure(
            SCRIPT_TOO_LARGE,
            f"script {len(raw)}B > {limits.max_script_bytes}B",
        )

    out: list[Instruction] = []
    i = 0
    n = len(raw)
    # 解析期用静态括号深度检查：栈元素记录 (是否已遇到 ELSE)
    branch_stack: list[bool] = []
    pc = 0

    while i < n:
        offset = i
        b = raw[i]
        i += 1

        if DIRECT_PUSH_MIN <= b <= DIRECT_PUSH_MAX:
            length = b
            if i + length > n:
                raise VerificationFailure(
                    MALFORMED_PUSH, f"direct push {length}B overruns script", pc=pc
                )
            data = raw[i : i + length]
            i += length
            _check_element(data, limits, pc)
            out.append(Instruction(b, data, pc, offset))
            pc += 1
            continue

        if b == OP_0:
            out.append(Instruction(b, b"", pc, offset))
            pc += 1
            continue

        if b in (OP_DATA_1, OP_DATA_2, OP_DATA_4):
            width = {OP_DATA_1: 1, OP_DATA_2: 2, OP_DATA_4: 4}[b]
            if i + width > n:
                raise VerificationFailure(MALFORMED_PUSH, "length prefix overruns", pc=pc)
            length = int.from_bytes(raw[i : i + width], "little")
            i += width
            # 最小编码：能用更短方式推送的，拒绝
            min_width = 1 if length <= 0xFF else 2 if length <= 0xFFFF else 4
            if width != min_width:
                raise VerificationFailure(
                    MALFORMED_PUSH, f"non-minimal push prefix for {length}B", pc=pc
                )
            # 最小编码：长度 <= 0x4B 必须直接推送
            if length <= DIRECT_PUSH_MAX:
                raise VerificationFailure(
                    MALFORMED_PUSH, f"push of {length}B must use direct opcode", pc=pc
                )
            if i + length > n:
                raise VerificationFailure(MALFORMED_PUSH, "push data overruns script", pc=pc)
            data = raw[i : i + length]
            i += length
            _check_element(data, limits, pc)
            out.append(Instruction(b, data, pc, offset))
            pc += 1
            continue

        # 经典编号中明确保留/禁用的字节：与“完全未知”区分开
        if b in _RESERVED_BYTES:
            raise VerificationFailure(RESERVED_OPCODE, f"0x{b:02X} is reserved", pc=pc)

        # 白名单
        spec = SUPPORTED.get(b)
        if spec is None:
            raise VerificationFailure(UNKNOWN_OPCODE, f"opcode 0x{b:02X} not whitelisted", pc=pc)

        # 解析期分支配对
        if b in (OP_IF, OP_NOTIF):
            if len(branch_stack) >= limits.max_script_depth:
                from ..errors import SCRIPT_DEPTH_EXCEEDED

                raise VerificationFailure(
                    SCRIPT_DEPTH_EXCEEDED,
                    f"branch depth > {limits.max_script_depth}",
                    pc=pc,
                )
            branch_stack.append(False)
        elif b == OP_ELSE:
            if not branch_stack or branch_stack[-1]:
                raise VerificationFailure(UNBALANCED_IF, "ELSE without IF or double ELSE", pc=pc)
            branch_stack[-1] = True
        elif b == OP_ENDIF:
            if not branch_stack:
                raise VerificationFailure(UNBALANCED_IF, "ENDIF without IF", pc=pc)
            branch_stack.pop()

        out.append(Instruction(b, b"", pc, offset))
        pc += 1

    if branch_stack:
        raise VerificationFailure(UNBALANCED_IF, "unterminated IF at end of script")
    return out


def _check_element(data: bytes, limits: Limits, pc: int) -> None:
    if len(data) > limits.max_element_size:
        from ..errors import ELEMENT_TOO_LARGE

        raise VerificationFailure(
            ELEMENT_TOO_LARGE,
            f"element {len(data)}B > {limits.max_element_size}B",
            pc=pc,
        )


def encode_push(data: bytes) -> bytes:
    """把任意字节串编码成最小推送脚本（供夹具/工具使用）。"""
    n = len(data)
    if n == 0:
        return bytes([OP_0])
    if n <= DIRECT_PUSH_MAX:
        return bytes([n]) + data
    if n <= 0xFF:
        return bytes([OP_DATA_1, n]) + data
    if n <= 0xFFFF:
        return bytes([OP_DATA_2]) + n.to_bytes(2, "little") + data
    return bytes([OP_DATA_4]) + n.to_bytes(4, "little") + data
