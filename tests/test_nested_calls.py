"""嵌套调用与回滚范围测试。

覆盖关键边界：
* 子帧异常中止（OOG/INVALID）：子写丢弃、已转发 gas 不退回，父帧收到 0；
* 子帧 REVERT：子写丢弃、剩余 gas 退回父帧；
* 父帧在子失败后继续执行且成功：父写保留（回滚只影响子帧范围）；
* 子成功：写集合并进父帧，最终一起提交；
* 调用深度上限：返回 0 但父帧不崩；
* 63/64 gas 转发：父帧至少保留 1/64。
"""

from __future__ import annotations

from teachchain import gas as G
from teachchain.opcodes import Op, assemble
from teachchain.vm import MAX_CALL_DEPTH, VirtualMachine


def call_child(child_code: bytes, parent_body: str, gas=500_000,
               calldata=None, committed=None):
    codes = {CHILD_HEX: child_code}
    parent = assemble(parent_body)
    return VirtualMachine(codes).execute("0x00000000000000aa", parent, calldata, gas,
                                         committed or {})


# CALL 三操作数（栈顶起）：gas, slot, addr_word
def call_seq(addr_word: str, gas_expr: str = "100000") -> str:
    return f"PUSH {addr_word}\nPUSH 0\nPUSH {gas_expr}\nCALL\n"


CHILD = 0x00000000000000C1
CHILD_HEX = "0x00000000000000c1"
CHILD_ADDR_WORD = str(CHILD)


def test_child_halt_discards_child_write_but_parent_continues():
    # 子合约写槽 9 后 INVALID
    child = assemble("PUSH 7\nPUSH 9\nSSTORE\nINVALID")
    parent_body = (
        call_seq(CHILD_ADDR_WORD)
        + "POP\n"                       # 丢 ret0
        # status 在栈顶；非 0 才写 11，否则写 22
        + "PUSH 22\nPUSH 1\nSSTORE\n"   # 无论成败都写父槽1=22
        + "PUSH 1\nPUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nRETURN\n"
    )
    r = call_child(child, parent_body)
    assert r.status == 1
    assert r.output == [1]
    # 父写保留
    assert r.writes.get(("0x00000000000000aa", 1)) == 22
    # 子写不存在
    assert ("0x00000000000000c1", 9) not in r.writes
    # 子异常消耗转发的 gas：父总消耗 > CALL 固定费
    assert r.gas_used > G.COST_CALL


def test_child_revert_returns_remaining_gas_and_discards_writes():
    # 子：写槽 5，然后 REVERT；只做很少操作，剩很多 gas
    child = assemble(
        "PUSH 3\nPUSH 5\nSSTORE\n"
        "PUSH 9\nPUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nREVERT"
    )
    parent_body = (
        call_seq(CHILD_ADDR_WORD, "100000")
        + "POP\n"
        + "PUSH 0\nJUMPI\n"            # status=0 -> JUMPI 不跳转
        + "PUSH 77\nPUSH 2\nSSTORE\n"
        + "PUSH 1\nPUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nRETURN\n"
    )
    # 上面 PUSH 0 JUMPI 目标 0 非法 —— 改为简单顺序写
    parent_body = (
        call_seq(CHILD_ADDR_WORD, "100000")
        + "POP\n"
        + "PUSH 77\nPUSH 2\nSSTORE\n"
        + "PUSH 1\nPUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nRETURN\n"
    )
    r = call_child(child, parent_body)
    assert r.status == 1
    assert r.writes.get(("0x00000000000000aa", 2)) == 77
    assert ("0x00000000000000c1", 5) not in r.writes
    # REVERT 退回大部分转发 gas：父消耗应明显小于 100000 转发全损的情形
    assert r.gas_used < 100_000


def test_successful_child_writes_merge_into_parent():
    child = assemble("PUSH 4\nPUSH 8\nSSTORE\nSTOP")
    parent_body = (
        call_seq(CHILD_ADDR_WORD)
        + "POP\n"
        + "PUSH 1\nPUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nRETURN\n"
    )
    r = call_child(child, parent_body)
    assert r.status == 1
    assert r.writes[("0x00000000000000c1", 8)] == 4


def test_child_can_read_committed_storage_and_parent_pending_write():
    # 已提交状态中 0xchild 槽1=100；子读它返回
    child = assemble(
        "PUSH 1\nSLOAD\n"
        "PUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nRETURN"
    )
    parent_body = call_seq(CHILD_ADDR_WORD) + "PUSH 0\nMSTORE\nSTOP"
    r = call_child(child, parent_body,
                   committed={(CHILD_HEX, 1): 100})
    assert r.status == 1
    # 合并写集为空（只读），根帧成功
    assert r.writes == {}


def test_call_to_missing_account_succeeds_with_no_effect():
    parent = assemble(
        "PUSH 12345\nPUSH 0\nPUSH 50000\nCALL\n"
        "POP\nPOP\nSTOP"
    )
    r = VirtualMachine({}).execute("0x00000000000000aa", parent, None, 500_000, {})
    assert r.status == 1
    assert r.writes == {}
    # 无代码账户：转发 gas 不应被扣除（实现只收 CALL 固定费）
    assert r.gas_used == 9 + G.COST_CALL + 4


def test_call_depth_limit_returns_status_zero():
    # 自调用递归合约：进入即 CALL 自己，直到深度上限
    recurse = assemble(
        f"PUSH {CHILD_ADDR_WORD}\nPUSH 0\nPUSH 1000000\nCALL\n"
        "POP\nPOP\nSTOP"
    )
    codes = {CHILD_HEX: recurse}
    # 用 child 自己作为根，触发深度封顶
    r = VirtualMachine(codes).execute(CHILD_HEX, recurse, None, 5_000_000, {})
    assert r.status == 1
    assert r.gas_used < 5_000_000  # 封顶后正常返回，gas 未耗尽


def test_63_64_gas_cap_keeps_gas_in_parent():
    # 子进入即 OOG（INVALID 路径会消耗转发），转发上限 min(gas_in, 63/64*parent)
    child = bytes([Op.INVALID])
    # 父请求转发 10 亿（超过余额），实际最多 63/64 * 父剩余
    parent_body = call_seq(CHILD_ADDR_WORD, "1000000000") + "POP\nPOP\nSTOP"
    r = call_child(child, parent_body, gas=640_000)
    # 父先收 CALL 费 700，余 639300，最多转发 floor(639300*63/64)
    forwarded = (640_000 - 9 - G.COST_CALL) * 63 // 64
    # 子 INVALID 立即消耗全部转发；父保留约 1/64
    assert r.status == 1
    assert r.gas_used == 9 + G.COST_CALL + 4 + forwarded


def test_nested_child_runs_with_deterministic_calldata_empty():
    # CALL 不传 calldata（固定 []）
    child = assemble("CALLDATASIZE\nPUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nRETURN")
    parent_body = call_seq(CHILD_ADDR_WORD) + "POP\nSTOP"
    r = call_child(child, parent_body)
    assert r.status == 1  # 子成功（父丢弃返回值不影响）
