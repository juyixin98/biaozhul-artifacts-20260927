"""整数边界与失败类别测试：断言具体结果值与具体 halt 类别。

栈序统一采用 EVM 约定：所有二元指令「先弹出的栈顶 a 为第一操作数」，
例如 SUB 计算 a - b、LT 计算 a < b、MSTORE 栈顶是 offset、SSTORE 栈顶是 key。
"""

from __future__ import annotations

import pytest

from teachchain import gas as G
from teachchain.opcodes import Op, assemble, decode
from teachchain.vm import MASK64, VirtualMachine

MAXU = MASK64


def run(code, gas=500_000, calldata=None, committed=None, codes=None,
        address="0xt"):
    return VirtualMachine(codes or {}).execute(
        address, code, calldata, gas, committed or {}
    )


def ret_mem0(value_asm: str) -> bytes:
    # value 已在栈顶：MSTORE 栈顶需是 offset，故 DUP/PUSH0 后 MSTORE，
    # 再 RETURN：length=1 先压，offset=0 在栈顶
    return assemble(
        value_asm
        + "\nPUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nRETURN\n"
    )


def test_addition_wraps_mod_2_64():
    assert run(ret_mem0(f"PUSH {MAXU}\nPUSH 1\nADD")).output == [0]
    # (2^64-1) + (2^64-1) -> 2^64 - 2
    assert run(ret_mem0(f"PUSH {MAXU}\nPUSH {MAXU}\nADD")).output == [MAXU - 1]


def test_subtraction_wraps_mod_2_64():
    # 计算 a - b 时先压 b 再压 a（a 在栈顶）
    assert run(ret_mem0("PUSH 1\nPUSH 0\nSUB")).output == [MAXU]   # 0 - 1
    assert run(ret_mem0(f"PUSH {MAXU}\nPUSH 5\nSUB")).output == [6]  # 5 - (2^64-1)


def test_multiplication_wraps_mod_2_64():
    # a=2^64-1（栈顶）, b=2：(2^64-1)*2 mod 2^64 = 2^64-2
    assert run(ret_mem0(f"PUSH 2\nPUSH {MAXU}\nMUL")).output == [MAXU - 1]
    assert run(ret_mem0(f"PUSH 2\nPUSH {2**63}\nMUL")).output == [0]


def test_division_and_modulo_by_zero_return_zero_not_halt():
    # a / b：先压 b 再压 a
    assert run(ret_mem0("PUSH 7\nPUSH 0\nDIV")).output == [0]   # 0/7
    assert run(ret_mem0("PUSH 7\nPUSH 0\nMOD")).output == [0]
    assert run(ret_mem0("PUSH 3\nPUSH 17\nDIV")).output == [5]  # 17/3
    assert run(ret_mem0("PUSH 3\nPUSH 17\nMOD")).output == [2]  # 17%3
    # 除以 0（b=0）得 0 而非 halt
    assert run(ret_mem0("PUSH 0\nPUSH 7\nDIV")).output == [0]
    assert run(ret_mem0("PUSH 0\nPUSH 7\nMOD")).output == [0]


def test_comparison_results_are_zero_or_one():
    # a <op> b：先压 b 再压 a（a 在栈顶）
    assert run(ret_mem0("PUSH 2\nPUSH 1\nLT")).output == [1]  # 1 < 2
    assert run(ret_mem0("PUSH 1\nPUSH 2\nLT")).output == [0]  # 2 < 1
    assert run(ret_mem0("PUSH 1\nPUSH 2\nGT")).output == [1]  # 2 > 1
    assert run(ret_mem0("PUSH 2\nPUSH 2\nEQ")).output == [1]
    assert run(assemble("PUSH 0\nISZERO\nPUSH 0\nMSTORE\n"
                        "PUSH 1\nPUSH 0\nRETURN")).output == [1]


def test_stack_underflow_has_dedicated_halt_code():
    r = run(assemble("ADD\nSTOP"))
    assert r.status == 0 and r.halt_code == "stack_underflow"


def test_stack_overflow_has_dedicated_halt_code():
    prog = "\n".join(["PUSH 1"] * (G.MAX_STACK + 1)) + "\nSTOP"
    r = run(assemble(prog), gas=10_000_000)
    assert r.status == 0 and r.halt_code == "stack_overflow"


def test_invalid_opcode_byte_halt():
    assert run(bytes([Op.INVALID])).halt_code == "invalid_instruction"
    # 0x0F 未定义，decode 阶段即拒绝（部署准入层会拒绝上链）
    with pytest.raises(ValueError):
        decode(bytes([0x0F]))


def test_truncated_immediate_rejected_at_decode():
    # PUSH 后只有 3 字节立即数，解码应失败
    with pytest.raises(ValueError):
        decode(bytes([Op.PUSH, 0, 0, 0]))


def test_jump_to_non_jumpdest_halt():
    r = run(assemble("PUSH 0\nJUMP\nSTOP"))
    assert r.status == 0 and r.halt_code == "invalid_jump"


def test_jumpi_conditional_branch_executes():
    # EVM JUMPI：condition=次顶, destination=栈顶
    code = assemble(
        "PUSH 0\nPUSH skip\nJUMPI\n"   # cond=0 不跳转，顺序落到 skip 前的成功返回
        "PUSH 11\nPUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nRETURN\n"
        "skip:\nJUMPDEST\nPUSH 22\nPUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nRETURN\n"
    )
    assert run(code).output == [11]
    code2 = assemble(
        "PUSH 7\nPUSH skip\nJUMPI\n"
        "PUSH 11\nPUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nRETURN\n"
        "skip:\nJUMPDEST\nPUSH 22\nPUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nRETURN\n"
    )
    assert run(code2).output == [22]


def test_calldataload_missing_index_is_zero():
    miss = assemble("PUSH 5\nCALLDATALOAD\nPUSH 0\nMSTORE\n"
                    "PUSH 1\nPUSH 0\nRETURN")
    assert run(miss, calldata=[9]).output == [0]
    assert run(miss, calldata=[]).output == [0]
    hit = assemble("PUSH 0\nCALLDATALOAD\nPUSH 0\nMSTORE\n"
                   "PUSH 1\nPUSH 0\nRETURN")
    assert run(hit, calldata=[9]).output == [9]


def test_return_range_outside_memory_halt():
    # length=2(次顶), offset=0(栈顶)：未扩张内存 -> 越界
    r = run(assemble("PUSH 2\nPUSH 0\nRETURN"))
    assert r.status == 0 and r.halt_code == "invalid_return_range"
    # length 超过上限 64
    too_long = assemble("PUSH 1\nPUSH 0\nMSTORE\nPUSH 65\nPUSH 0\nRETURN")
    assert run(too_long).halt_code == "invalid_return_range"


def test_fall_through_end_is_success():
    # 程序不以 STOP/RETURN 结尾也视为成功空输出
    r = run(assemble("PUSH 1\nPOP"))
    assert r.status == 1 and r.output == []
