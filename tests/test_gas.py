"""gas 计费测试：每笔费用与内存扩张公式都有**手工推导的确定值**。

这些期望值来自 gas.py 的常量与公式手算（不调用被测实现生成）：
PUSH=3, MSTORE=3, MLOAD=3, ADD=5, SSTORE set=20000/reset=5000/noop=200,
内存价 memory_cost(w)=3w + w*w//512。
"""

from __future__ import annotations

from teachchain import gas as G
from teachchain.opcodes import assemble
from teachchain.vm import VirtualMachine


def run(code: bytes, gas: int, committed=None, calldata=None,
        address="0xsstore"):
    return VirtualMachine({}).execute(address, code, calldata, gas,
                                      committed or {})


def test_memory_cost_formula_hand_values():
    # 直接校验手算表
    assert G.memory_cost(0) == 0
    assert G.memory_cost(1) == 3          # 3*1 + 1//512
    assert G.memory_cost(16) == 48
    assert G.memory_cost(512) == 2048     # 3*512 + 512*512//512 = 1536+512
    assert G.memory_cost(1024) == 5120    # 3072 + 2048
    # 扩张增量
    assert G.memory_expansion_delta(0, 1) == 3
    assert G.memory_expansion_delta(1, 1) == 0
    assert G.memory_expansion_delta(0, 512) == 2048
    assert G.memory_expansion_delta(511, 512) == 2048 - (3 * 511 + 511 * 511 // 512)


def test_mstore_charges_opcode_and_memory_expansion():
    # 栈布局 [value(次顶), offset(栈顶)]：PUSH 7(值) PUSH 0(偏移)
    code = assemble("PUSH 7\nPUSH 0\nMSTORE\nSTOP")
    r = run(code, 100)
    assert r.status == 1
    assert r.gas_used == 3 + 3 + 3 + 3
    # 第二次仍写槽 0，不产生新内存扩张，仅再收操作码费
    code2 = assemble("PUSH 7\nPUSH 0\nMSTORE\nPUSH 9\nPUSH 0\nMSTORE\nSTOP")
    r2 = run(code2, 100)
    assert r2.gas_used == (3 + 3 + 3 + 3) + (3 + 3 + 3)


def test_mstore_grows_memory_when_needed():
    # mem[4] 需扩张到 5 个字：内存费 memory_cost(5)=15
    # 栈序：value=1 先压，offset=4 在栈顶
    code = assemble("PUSH 1\nPUSH 4\nMSTORE\nSTOP")
    r = run(code, 100)
    assert r.status == 1
    assert r.gas_used == 3 + 3 + 3 + 15


def test_exact_gas_then_success():
    # PUSH(3)+PUSH(3)+STOP(0)=6；给 6 恰好成功，给 5 在首个 PUSH 即 OOG
    code = assemble("PUSH 1\nPUSH 2\nSTOP")
    assert run(code, 6).status == 1
    short = run(code, 5)
    assert short.status == 0
    assert short.halt_code == "out_of_gas"
    assert short.gas_used == 5  # 已预扣的全部消耗


def test_gas_checked_before_side_effect_sstore():
    # 只剩 10 gas 时执行 SSTORE（需 20000）：必须 OOG，写集为空。
    # 程序先用若干 PUSH 把 gas 压到 10：PUSH*2 = 6，SSTORE 固定费 0，
    # 动态费 20000 不足 -> halt；因此给 16：PUSH 10 + SSTORE 时余 10。
    code = assemble("PUSH 42\nPUSH 1\nSSTORE\nSTOP")
    r = run(code, 16)
    assert r.status == 0
    assert r.halt_code == "out_of_gas"
    assert r.writes == {}
    assert r.gas_used == 16


def test_sstore_set_reset_noop_gas_and_clear_refund():
    addr = "0xsstore"
    code = assemble("PUSH 5\nPUSH 0\nSSTORE\nSTOP")       # 0 -> 5
    r1 = run(code, 100_000, committed={})
    assert r1.status == 1
    # PUSH 3 + PUSH 3 + SSTORE(set) 20000 + STOP 0
    assert r1.gas_used == 3 + 3 + G.SSTORE_SET
    # SSTORE 弹 (slot=栈顶, value=次顶)：写的是槽 0 = 5
    assert r1.writes[(addr, 0)] == 5

    committed = {(addr, 0): 5}
    code_reset = assemble("PUSH 8\nPUSH 0\nSSTORE\nSTOP")  # 5 -> 8
    r2 = VirtualMachine({}).execute(addr, code_reset, None, 100_000, committed)
    assert r2.gas_used == 3 + 3 + G.SSTORE_RESET

    code_clear = assemble("PUSH 0\nPUSH 0\nSSTORE\nSTOP")  # 5 -> 0
    r3 = VirtualMachine({}).execute(addr, code_clear, None, 100_000, committed)
    assert r3.gas_used == 3 + 3 + G.SSTORE_RESET
    assert r3.gas_refund == G.SSTORE_CLEAR_REFUND

    code_noop = assemble("PUSH 5\nPUSH 0\nSSTORE\nSTOP")   # 5 -> 5
    r4 = VirtualMachine({}).execute(addr, code_noop, None, 100_000, committed)
    assert r4.gas_used == 3 + 3 + G.SSTORE_NOOP


def test_vm_raw_refund_after_many_sets():
    # 多次 set 后一次清零：无论中间花了多少，VM 只记录一次原始退款 15000；
    # 1/2 截断是 kernel 结算层的职责（见 test_kernel.py）。
    lines = []
    for slot in range(5):
        lines += [f"PUSH 1", f"PUSH {slot}", "SSTORE"]
    lines += ["PUSH 0", "PUSH 0", "SSTORE", "STOP"]
    code = assemble("\n".join(lines))
    r = run(code, 1_000_000)
    assert r.status == 1
    # 5 次 set = 100000，第 6 次清零=5000，操作费很少；
    # 名义退款 15000 远小于 vm_used//2，故不被截断
    assert r.gas_refund == G.SSTORE_CLEAR_REFUND

def test_vm_refund_is_raw_capping_happens_in_kernel():
    # VM 返回原始清零退款（15000），1/2 截断在 kernel 结算层做（见 test_kernel）。
    code2 = assemble("PUSH 0\nPUSH 0\nSSTORE\nSTOP")
    r2 = VirtualMachine({}).execute("0xa", code2, None, 100_000,
                                    {("0xa", 0): 9})
    assert r2.gas_used == 3 + 3 + G.SSTORE_RESET
    assert r2.gas_refund == G.SSTORE_CLEAR_REFUND


def test_intrinsic_gas_table():
    assert G.intrinsic_gas("invoke", input_len=0) == 21_000
    assert G.intrinsic_gas("invoke", input_len=3) == 21_000 + 12
    assert G.intrinsic_gas("deploy", code_len=10) == 21_000 + 32_000 + 40
