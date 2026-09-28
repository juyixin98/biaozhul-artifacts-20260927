"""栈机子包。"""

from .stack_machine import (
    ExecResult,
    RunContext,
    StackMachine,
    cast_bool,
    decode_smallint,
    encode_smallint,
)

__all__ = [
    "ExecResult",
    "RunContext",
    "StackMachine",
    "cast_bool",
    "decode_smallint",
    "encode_smallint",
]
