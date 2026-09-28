"""虚拟机执行失败类别（收据的 error_category 取这些稳定字符串）。"""
from __future__ import annotations

from enum import StrEnum


class Failure(StrEnum):
    INVALID_BYTECODE = "INVALID_BYTECODE"          # 未知操作码 / 立即数截断（静态拒绝，不扣帧 gas 之外费用）
    OUT_OF_GAS = "OUT_OF_GAS"                      # 剩余 gas 不足
    STACK_UNDERFLOW = "STACK_UNDERFLOW"
    STACK_OVERFLOW = "STACK_OVERFLOW"
    INTEGER_OVERFLOW = "INTEGER_OVERFLOW"          # ADD/SUB/MUL 结果越出 64 位有符号
    DIV_BY_ZERO = "DIV_BY_ZERO"                    # DIV/MOD/ADDMOD/MULMOD 除数为 0
    INVALID_MEMORY = "INVALID_MEMORY"              # 负偏移 / 末端超教学上限
    REVERTED = "REVERTED"                          # 显式 REVERT
    CALL_DEPTH_EXCEEDED = "CALL_DEPTH_EXCEEDED"
