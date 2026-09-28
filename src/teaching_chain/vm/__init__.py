"""确定性栈式虚拟机：操作码、gas 计费与执行引擎。"""
from __future__ import annotations

from .assembler import assemble, disassemble
from .errors import Failure
from .machine import ExecutionResult, VMError, execute
from .opcodes import InvalidBytecode, Op, validate_bytecode

__all__ = [
    "Op",
    "Failure",
    "VMError",
    "InvalidBytecode",
    "ExecutionResult",
    "execute",
    "validate_bytecode",
    "assemble",
    "disassemble",
]
