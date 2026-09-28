"""临界 gas 行为。

核验点：

* 每步**先检查并扣除 gas、再执行副作用**：用“恰好差一点”的预算证明
  SSTORE / SLOAD / 内存扩张在费用不足时完全不发生；
* 内存扩张按 32 字节字计费 ``3w + w²/512``，扩张只收增量；
* 成功时只收实际费用，异常停机收整帧预算；
* 期望值来自 conftest 的手写常量与独立内存费用参考，而非从 gas 模块读回。
"""
from __future__ import annotations

import pytest

from teaching_chain.vm import Failure, execute
from teaching_chain.vm.gas import intrinsic_gas, memory_expansion_cost

from .conftest import (
    EXPECTED_FEE,
    EXPECTED_NONZERO_BYTE,
    EXPECTED_TX_MINIMUM,
    EXPECTED_ZERO_BYTE,
    asm,
    reference_intrinsic,
    reference_memory_cost,
)


def code(text: str) -> bytes:
    return bytes.fromhex(asm(text))


def test_gas_constants_match_documented_table():
    # 防止价格表被悄悄修改：测试锁死 README 中的教学价格
    from teaching_chain.vm import gas as g
    assert g.G_SSTORE == EXPECTED_FEE["SSTORE"]
    assert g.G_LOW == EXPECTED_FEE["SLOAD"]
    assert g.G_VERY_LOW == EXPECTED_FEE["ADD"]
    assert g.G_BASE == EXPECTED_FEE["POP"]
    assert g.G_CALL_BASE == EXPECTED_FEE["CALL_BASE"]
    assert g.G_TX_MINIMUM == EXPECTED_TX_MINIMUM
    assert g.G_TX_ZERO_BYTE == EXPECTED_ZERO_BYTE
    assert g.G_TX_NONZERO_BYTE == EXPECTED_NONZERO_BYTE


def test_exact_gas_succeeds_and_consumes_all():
    # PUSH8 3 + PUSH8 2 + ADD + STOP = 3+3+3+0 = 9
    prog = code("PUSH8 3\nPUSH8 2\nADD\nSTOP")
    r = execute(prog, 9)
    assert r.ok, r.error_category
    assert r.gas_used == 9
    assert r.return_value == 5


def test_one_gas_short_fails_before_effect_and_charges_full_frame():
    # 同样程序给 8：第三条 ADD 执行前余额 2 < 3 => OOG，ADD 不发生
    prog = code("PUSH8 3\nPUSH8 2\nADD\nSTOP")
    r = execute(prog, 8)
    assert not r.ok
    assert r.error_category == str(Failure.OUT_OF_GAS)
    assert r.error_pc >= 0
    # 整帧预算保留（费用消耗规则），栈/内存回滚为空
    assert r.gas_used == 8
    assert r.stack == []


def test_sstore_not_attempted_when_gas_insufficient():
    # SSTORE 费 20。余额恰好 19：写绝不能发生，且必须是 OOG
    prog = code("PUSH8 7\nPUSH8 1\nSSTORE\nSTOP")
    # PUSH+PUSH 各 3 => 到 SSTORE 前剩 19 - 6 = 13
    r = execute(prog, 19, storage={})
    assert not r.ok
    assert r.error_category == str(Failure.OUT_OF_GAS)
    assert r.storage == {}  # 写后异常回滚（此处写从未发生，也必须为空）


def test_sstore_exact_gas_writes_then_reverts_on_later_failure():
    # 写成功后，后续 DIV 除零：存储写入必须随帧回滚
    prog = code(
        "PUSH8 7\nPUSH8 1\nSSTORE\n"
        "PUSH8 1\nPUSH8 0\nDIV\nSTOP"
    )
    r = execute(prog, 100_000, storage={})
    assert not r.ok
    assert r.error_category == str(Failure.DIV_BY_ZERO)
    assert r.storage == {}                 # 状态回滚
    assert r.gas_used == 100_000           # 费用保留


def test_sstore_success_charges_and_persists():
    prog = code("PUSH8 77\nPUSH8 3\nSSTORE\nPUSH8 3\nSLOAD\nSTOP")
    r = execute(prog, 100_000, storage={})
    assert r.ok, r.error_category
    assert r.storage == {3: 77}
    assert r.return_value == 77
    # PUSH8 77(3) PUSH8 3(3) SSTORE(20) PUSH8 3(3) SLOAD(5) STOP(0) = 34
    assert r.gas_used == 34


def test_memory_expansion_first_word_cost():
    # MSTORE offset=0：扩张 1 个字，费用 3*1 + 1//512 = 3
    prog = code("PUSH8 99\nPUSH8 0\nMSTORE\nSTOP")
    # 指令费：3+3+3 = 9；扩张 3 => 12
    r = execute(prog, 12, storage={})
    assert r.ok, r.error_category
    assert bytes.fromhex(r.memory_hex)[:32] == (99).to_bytes(32, "big", signed=True)
    assert r.gas_used == 12


def test_memory_expansion_one_short_means_no_write():
    prog = code("PUSH8 99\nPUSH8 0\nMSTORE\nSTOP")
    r = execute(prog, 11)  # 9 指令费 + 扩张只有 2
    assert not r.ok
    assert r.error_category == str(Failure.OUT_OF_GAS)
    assert r.memory_hex == ""


def test_memory_expansion_second_word_is_incremental_only():
    # 先在字 0 写，再在 offset=32（字 1）写：
    # 第二次扩张 1->2 个字，增量 = total(2)-total(1) = (6+0)-(3+0) = 3
    prog = code(
        "PUSH8 1\nPUSH8 0\nMSTORE\n"
        "PUSH8 2\nPUSH8 32\nMSTORE\nSTOP"
    )
    r = execute(prog, 100_000)
    assert r.ok, r.error_category
    # 指令费 6 个 VERY_LOW = 18；扩张 3+3 = 6；共 24
    assert r.gas_used == 24
    mem = bytes.fromhex(r.memory_hex)
    assert int.from_bytes(mem[0:32], "big", signed=True) == 1
    assert int.from_bytes(mem[32:64], "big", signed=True) == 2


@pytest.mark.parametrize("prev,new", [(0, 1), (1, 2), (0, 16), (16, 17), (100, 200)])
def test_memory_cost_formula_matches_independent_reference(prev, new):
    got = memory_expansion_cost(prev, new)
    assert got == reference_memory_cost(prev, new)
    # 手写几个确定值
    if (prev, new) == (0, 1):
        assert got == 3
    if (prev, new) == (0, 16):
        # 3*16 + 256//512 = 48
        assert got == 48


def test_quadratic_term_kicks_in_at_512_words():
    # 0 -> 512 字：3*512 + 512*512//512 = 1536 + 512 = 2048
    assert memory_expansion_cost(0, 512) == 2048


def test_negative_memory_offset_rejected():
    prog = code("PUSH8 1\nPUSH8 -4\nMSTORE\nSTOP")
    r = execute(prog, 100_000)
    assert not r.ok
    assert r.error_category == str(Failure.INVALID_MEMORY)


def test_mload_never_written_reads_zero():
    prog = code("PUSH8 64\nMLOAD\nSTOP")
    r = execute(prog, 100_000)
    assert r.ok
    assert r.return_value == 0
    # offset 64..95 覆盖第 3 个字；指令费 3+3，扩张费由独立参考给出
    assert r.gas_used == 3 + 3 + reference_memory_cost(0, 3)


def test_mload_gas_expectation():
    prog = code("PUSH8 64\nMLOAD\nSTOP")
    r = execute(prog, 100_000)
    assert r.ok
    expected_instr = 3 + 3  # PUSH8 + MLOAD
    expected_expand = reference_memory_cost(0, 3)  # offset 64..95 -> 3 字
    assert r.gas_used == expected_instr + expected_expand


@pytest.mark.parametrize("data,expected", [
    (b"\x00", 21 + 1),
    (b"\x01", 21 + 4),
    (b"\x00\x00\x01", 21 + 1 + 1 + 4),
])
def test_intrinsic_gas_byte_wise(data, expected):
    assert intrinsic_gas(data) == expected
    assert reference_intrinsic(data) == expected


def test_stack_underflow_detected():
    r = execute(code("PUSH8 1\nADD\nSTOP"), 100_000)
    assert not r.ok
    assert r.error_category == str(Failure.STACK_UNDERFLOW)


def test_stack_overflow_detected():
    # DUP1 直到超过 1024
    prog = code("PUSH8 1\n" + "DUP1\n" * 1024 + "STOP")
    r = execute(prog, 1_000_000)
    assert not r.ok
    assert r.error_category == str(Failure.STACK_OVERFLOW)
