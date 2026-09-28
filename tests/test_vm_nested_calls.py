"""嵌套 CALL 语义：

* 子帧存储**隔离**：子帧 SSTORE 不泄漏到父帧（教学 VM 是无参数沙箱）；
* 子帧异常（OOG / REVERT / 除零 / 溢出）：子帧改动丢弃，父帧失败回滚，
  子调用分走的 gas 全部消耗，不退还；
* 子帧成功：未使用 gas 退还父帧；
* 嵌套深度上限类别明确。
"""
from __future__ import annotations

from teaching_chain.vm import Failure, execute

from .conftest import asm


def c(text: str) -> bytes:
    return bytes.fromhex(asm(text))


SUCCESS_CHILD = """
PUSH8 55
PUSH8 9
SSTORE
PUSH8 1
PUSH8 1
ADD
STOP
"""


def test_successful_child_returns_unused_gas_and_isolates_storage():
    prog = c(f"""
PUSH8 42
PUSH8 1
SSTORE
CALL
{{
{SUCCESS_CHILD}
}}
PUSH8 1
SLOAD
STOP
""")
    # 父帧先写 storage[1]=42；子帧写 storage[9]=55（隔离，不影响父帧）
    r = execute(prog, 100_000, storage={})
    assert r.ok, r.error_category
    # 父帧读 storage[1] 仍是 42；键 9 不存在
    assert r.return_value == 42
    assert r.storage == {1: 42}


def test_child_oog_consumes_child_gas_and_rolls_back_parent_state():
    # 父帧先写一个存储，再调用一个预算不足的子帧
    # 给父帧一个紧凑预算：父 SSTORE(20) 后所余 gas 全部进子帧，
    # 子帧两条 PUSH8(6) 后 SSTORE 需要 20 => 用 100 预算使子帧刚好 OOG
    prog = c("""
PUSH8 42
PUSH8 1
SSTORE
CALL
{
PUSH8 55
PUSH8 9
SSTORE
STOP
}
STOP
""")
    # 费用：PUSH+PUSH+SSTORE = 26；CALL base 10；子帧拿到 100-36=64，
    # 子帧内 PUSH+PUSH=6，SSTORE 需 20，剩 58 足够……改为极小预算让子帧 OOG
    r = execute(prog, 45)  # 26 后余 19；CALL base 10 后子帧仅 9 gas
    assert not r.ok
    assert r.error_category == str(Failure.OUT_OF_GAS)
    # 父帧已执行的 SSTORE 随顶层失败回滚
    assert r.storage == {}
    # 整帧预算消耗
    assert r.gas_used == 45


def test_child_revert_propagates_and_child_state_discarded():
    prog = c("""
PUSH8 42
PUSH8 1
SSTORE
CALL
{
PUSH8 7
PUSH8 2
SSTORE
REVERT
}
STOP
""")
    r = execute(prog, 100_000)
    assert not r.ok
    assert r.error_category == str(Failure.REVERTED)
    assert r.storage == {}  # 父写入与子写入全部回滚
    assert r.gas_used == 100_000


def test_child_div_by_zero_category_propagates():
    prog = c("""
CALL
{
PUSH8 1
PUSH8 0
DIV
STOP
}
STOP
""")
    r = execute(prog, 100_000)
    assert not r.ok
    assert r.error_category == str(Failure.DIV_BY_ZERO)


def test_nested_child_success_inside_parent_computation():
    # 子帧成功（无存储效果），父帧继续运算
    prog = c("""
PUSH8 10
PUSH8 5
CALL
{
PUSH8 1
PUSH8 2
ADD
POP
STOP
}
SUB
STOP
""")
    r = execute(prog, 100_000)
    assert r.ok, r.error_category
    assert r.return_value == 5  # 10 - 5


def test_inner_child_failure_rolls_back_outer_writes_too():
    # 外层写 storage[1]，中层写 storage[2]，内层除零
    prog = c("""
PUSH8 100
PUSH8 1
SSTORE
CALL
{
PUSH8 200
PUSH8 2
SSTORE
CALL
{
PUSH8 1
PUSH8 0
MOD
STOP
}
STOP
}
STOP
""")
    r = execute(prog, 100_000)
    assert not r.ok
    assert r.error_category == str(Failure.DIV_BY_ZERO)
    assert r.storage == {}
    assert r.gas_used == 100_000


def test_call_depth_limit_category():
    # 手工构造 9 层嵌套 CALL（每层都包一个子块）
    inner = "PUSH8 1\nPUSH8 2\nADD\nPOP\nSTOP"

    def wrap(body: str) -> str:
        return f"CALL\n{{\n{body}\n}}"

    text = inner
    for _ in range(9):  # 深度 1..9；上限 8
        text = wrap(text)
    r = execute(c(text), 100_000)
    assert not r.ok
    assert r.error_category == str(Failure.CALL_DEPTH_EXCEEDED)


def test_depth_at_limit_succeeds():
    inner = "PUSH8 1\nPUSH8 2\nADD\nPOP\nSTOP"

    def wrap(body: str) -> str:
        return f"CALL\n{{\n{body}\n}}"

    text = inner
    for _ in range(7):  # 最深第 8 层
        text = wrap(text)
    r = execute(c(text), 100_000)
    assert r.ok, r.error_category


def test_trace_contains_child_steps_indented():
    prog = c("""
CALL
{
PUSH8 1
PUSH8 2
ADD
POP
STOP
}
STOP
""")
    r = execute(prog, 100_000, trace=True)
    assert r.ok
    joined = "\n".join(r.trace)
    assert "CALL depth=1 ok=True" in joined
    assert any(line.startswith("  ") for line in r.trace)
