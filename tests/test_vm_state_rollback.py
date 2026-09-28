"""状态写后异常的回滚、无效字节码拒绝、存储键/值边界。"""
from __future__ import annotations

import pytest

from teaching_chain.vm import Failure, execute

from .conftest import asm


def c(text: str) -> bytes:
    return bytes.fromhex(asm(text))


def test_write_then_overflow_reverts_only_state_not_fees():
    storage_before = {5: 999}
    prog = c("""
PUSH8 11
PUSH8 7
SSTORE
PUSH8 9223372036854775807
PUSH8 1
ADD
STOP
""")
    r = execute(prog, 100_000, storage=storage_before)
    assert not r.ok
    assert r.error_category == str(Failure.INTEGER_OVERFLOW)
    # 存储恢复到执行前（原有键 5 保持，新键 7 不存在）
    assert r.storage == {5: 999}
    assert 7 not in r.storage
    # gas 全耗
    assert r.gas_used == 100_000


def test_multiple_writes_all_reverted_on_revert():
    prog = c("""
PUSH8 1
PUSH8 1
SSTORE
PUSH8 2
PUSH8 2
SSTORE
PUSH8 3
PUSH8 3
SSTORE
REVERT
""")
    r = execute(prog, 100_000, storage={})
    assert not r.ok
    assert r.error_category == str(Failure.REVERTED)
    assert r.storage == {}


def test_successful_writes_partial_update_keeps_untouched_keys():
    prog = c("PUSH8 42\nPUSH8 2\nSSTORE\nSTOP")
    r = execute(prog, 100_000, storage={1: 10, 2: 20})
    assert r.ok
    assert r.storage == {1: 10, 2: 42}  # 键 2 被覆盖，键 1 保留


def test_sstore_same_key_twhen_then_failure_keeps_original():
    prog = c("""
PUSH8 100
PUSH8 8
SSTORE
PUSH8 200
PUSH8 8
SSTORE
PUSH8 1
PUSH8 0
DIV
STOP
""")
    r = execute(prog, 100_000, storage={8: 1})
    assert not r.ok
    assert r.storage == {8: 1}  # 两次写都回滚


def test_unknown_opcode_rejected_without_gas_consumption():
    code_bytes = bytes([0x7E])  # 未定义
    r = execute(code_bytes, 500)
    assert not r.ok
    assert r.error_category == str(Failure.INVALID_BYTECODE)
    assert r.gas_used == 0          # 静态拒绝：尚未开始执行
    assert r.error_pc == -1
    assert r.storage == {}


def test_truncated_push_immediate_rejected():
    r = execute(bytes([0x21, 0x00, 0x01]), 500)  # PUSH8 只有 2 字节
    assert not r.ok
    assert r.error_category == str(Failure.INVALID_BYTECODE)


def test_truncated_call_length_prefix_rejected():
    r = execute(bytes([0x60, 0x80]), 500)
    assert not r.ok
    assert r.error_category == str(Failure.INVALID_BYTECODE)


def test_call_inline_child_out_of_range_rejected():
    # CALL 声称 10 字节内联但实际只有 2
    r = execute(bytes([0x60, 0x0A, 0x00, 0x00]), 500)
    assert not r.ok
    assert r.error_category == str(Failure.INVALID_BYTECODE)


def test_unknown_opcode_nested_in_call_rejected():
    r = execute(bytes([0x60, 0x01, 0xEE, 0x00]), 500)
    assert not r.ok
    assert r.error_category == str(Failure.INVALID_BYTECODE)


def test_negative_storage_key_rejected():
    prog = c("PUSH8 1\nPUSH8 -1\nSSTORE\nSTOP")
    r = execute(prog, 100_000)
    assert not r.ok
    assert r.error_category == str(Failure.INVALID_MEMORY)


def test_large_storage_key_boundary_allowed():
    max_key = 2**63 - 1
    prog = c(f"PUSH8 1\nPUSH8 {max_key}\nSSTORE\nSTOP")
    r = execute(prog, 100_000)
    assert r.ok, r.error_category
    assert r.storage == {max_key: 1}


def test_zero_byte_code_is_natural_stop():
    r = execute(b"", 100)
    assert r.ok
    assert r.gas_used == 0
    assert r.return_value == 0


def test_storage_input_not_mutated():
    before = {1: 2}
    snapshot = dict(before)
    prog = c("PUSH8 3\nPUSH8 1\nSSTORE\nPUSH8 1\nPUSH8 0\nDIV\nSTOP")
    r = execute(prog, 100_000, storage=before)
    assert not r.ok
    assert before == snapshot  # 入参对象本身不被修改
    assert r.storage == snapshot
