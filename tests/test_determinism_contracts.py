"""确定性契约：执行内核不得接触宿主时间/随机源，且结果可重复。

这些测试是「行为约定 3（宿主时间随机数不可访问）」的可核验闸门：
静态扫描关键模块的源码与字节码导入，并对同一输入做重复执行比对。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from teachchain import gas as G
from teachchain.opcodes import assemble
from teachchain.vm import VirtualMachine

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "teachchain"

# 这些模块构成「确定性执行内核」，禁止导入宿主非确定源
DETERMINISTIC_MODULES = [
    "vm.py", "gas.py", "kernel.py", "opcodes.py", "errors.py",
    "models.py", "version.py", "replay.py",
]

FORBIDDEN_MODULES = {
    "time", "random", "secrets", "datetime", "uuid",
    "threading", "multiprocessing", "socket",
}
FORBIDDEN_CALLS = {"time.time", "time.time_ns", "datetime.now",
                   "datetime.utcnow", "random.random", "uuid.uuid4",
                   "secrets.token_"}


def _imports_of(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


@pytest.mark.parametrize("module", DETERMINISTIC_MODULES)
def test_kernel_modules_do_not_import_host_nondeterminism(module):
    mods = _imports_of(SRC / module)
    leaked = mods & FORBIDDEN_MODULES
    assert not leaked, f"{module} 引入了宿主非确定源: {leaked}"


def test_no_forbidden_calls_in_kernel_source():
    for module in DETERMINISTIC_MODULES:
        text = (SRC / module).read_text()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            # 粗粒度检查属性调用链文本
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                chain = f"{node.value.id}.{node.attr}"
                for bad in FORBIDDEN_CALLS:
                    assert not chain.startswith(bad), (
                        f"{module}: 禁止调用 {chain}"
                    )


def test_repeated_execution_identical_gas_writes_output():
    code = assemble(
        "PUSH 0\nCALLDATALOAD\nPUSH 0\nSLOAD\nADD\nDUP 1\nPUSH 0\nSSTORE\n"
        "PUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nRETURN"
    )
    first = None
    for _ in range(50):
        r = VirtualMachine({}).execute(
            "0xa", code, [7], 500_000, {("0xa", 0): 3})
        sig = (r.status, r.gas_used, r.gas_refund, tuple(r.output),
               tuple(sorted(r.writes.items())))
        if first is None:
            first = sig
        assert sig == first
    assert first[0] == 1 and first[3] == (10,)


def test_result_does_not_depend_on_object_identity_or_dict_order():
    # 用不同插入顺序构造 committed，结果必须一致（读路径有排序兜底）
    code = assemble("PUSH 0\nSLOAD\nPUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nRETURN")
    r1 = VirtualMachine({}).execute("0xa", code, None, 100_000,
                                    {("0xa", 0): 42})
    r2 = VirtualMachine({}).execute("0xa", code, None, 100_000,
                                    {("0xb", 1): 0, ("0xa", 0): 42})
    assert r1.output == r2.output == [42]
    assert r1.gas_used == r2.gas_used


def test_gas_table_is_pure_function():
    # 价格函数对相同输入恒定
    assert G.memory_cost(333) == G.memory_cost(333)
    assert G.sstore_cost(0, 5) == G.sstore_cost(0, 5)
    assert G.intrinsic_gas("invoke", input_len=2) == G.intrinsic_gas(
        "invoke", input_len=2)
