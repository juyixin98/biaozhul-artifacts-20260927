"""算术语义与 64 位整数边界。

期望值手写，并与 conftest 中的独立参考函数交叉比对——
不是从 VM 实现反推的“自证”。
"""
from __future__ import annotations

import pytest

from teaching_chain.vm import Failure, execute
from teaching_chain.vm.machine import INT64_MAX, INT64_MIN

from .conftest import (
    EXPECTED_FEE,
    asm,
    reference_add,
    reference_check_i64,
    reference_div,
    reference_truncating_divmod,
)


def run_arith(text, gas=100_000, storage=None):
    return execute(bytes.fromhex(asm(text)), gas, storage=storage)


def test_add_basic_independent_reference():
    # 手写：7 + 6 = 13
    r = run_arith("PUSH8 7\nPUSH8 6\nADD\nSTOP")
    assert r.ok, r.error_category
    assert r.return_value == 13
    # 与独立参考一致
    assert reference_add(7, 6) == 13


@pytest.mark.parametrize("a,b,expected", [
    (0, 0, 0),
    (1, -1, 0),
    (-5, -7, -12),
    (2**31, 2**31, 2**32),
    (INT64_MAX, 0, INT64_MAX),
    (INT64_MIN, 0, INT64_MIN),
    (INT64_MAX, -INT64_MAX, 0),
])
def test_add_table(a, b, expected):
    r = run_arith(f"PUSH8 {a}\nPUSH8 {b}\nADD\nSTOP")
    assert r.ok, f"{a}+{b}: {r.error_category}"
    assert r.return_value == expected
    assert reference_add(a, b) == expected


@pytest.mark.parametrize("a,b", [
    (INT64_MAX, 1),
    (INT64_MIN, -1),
    (INT64_MAX, INT64_MAX),
    (INT64_MIN, INT64_MIN),
])
def test_add_overflow_categorized(a, b):
    r = run_arith(f"PUSH8 {a}\nPUSH8 {b}\nADD\nSTOP")
    assert not r.ok
    assert r.error_category == str(Failure.INTEGER_OVERFLOW)
    assert r.error_pc >= 0
    # 溢出失败：费用保留，状态回滚
    assert r.gas_used == 100_000
    assert r.storage == {}
    # 参考实现同样判定越界
    assert reference_add(a, b) == "INTEGER_OVERFLOW"


@pytest.mark.parametrize("a,b,expected", [
    (13, 5, 8),
    (-13, 5, -18),
    (13, -5, 18),
    (INT64_MIN, 1, INT64_MIN - 1 if reference_check_i64(INT64_MIN - 1) else None),
])
def test_sub(a, b, expected):
    r = run_arith(f"PUSH8 {a}\nPUSH8 {b}\nSUB\nSTOP")
    if expected is None:
        assert not r.ok
        assert r.error_category == str(Failure.INTEGER_OVERFLOW)
    else:
        assert r.ok
        assert r.return_value == expected


def test_sub_overflow_min_minus_positive():
    r = run_arith(f"PUSH8 {INT64_MIN}\nPUSH8 1\nSUB\nSTOP")
    assert not r.ok
    assert r.error_category == str(Failure.INTEGER_OVERFLOW)


@pytest.mark.parametrize("a,b,expected", [
    (6, 7, 42),
    (-6, 7, -42),
    (INT64_MAX, 1, INT64_MAX),
    (INT64_MIN // 2, 2, (INT64_MIN // 2) * 2),
])
def test_mul(a, b, expected):
    r = run_arith(f"PUSH8 {a}\nPUSH8 {b}\nMUL\nSTOP")
    assert r.ok, r.error_category
    assert r.return_value == expected


def test_mul_overflow():
    r = run_arith("PUSH8 4611686018427387904\nPUSH8 2\nMUL\nSTOP")  # 2^62 * 2 = 2^63 越界
    assert not r.ok
    assert r.error_category == str(Failure.INTEGER_OVERFLOW)


@pytest.mark.parametrize("a,b", [
    (42, 0),
    (-42, 0),
    (INT64_MIN, 0),
])
def test_div_by_zero_categorized(a, b):
    r = run_arith(f"PUSH8 {a}\nPUSH8 {b}\nDIV\nSTOP")
    assert not r.ok
    assert r.error_category == str(Failure.DIV_BY_ZERO)
    assert reference_div(a, b) == "DIV_BY_ZERO"


@pytest.mark.parametrize("a,b,expected_q", [
    (42, 6, 7),
    (-42, 6, -7),
    (42, -6, -7),
    (-42, -6, 7),
    (43, 6, 7),   # 向零截断
    (-43, 6, -7),
    (INT64_MIN, -1, None),  # -2^63 / -1 = 2^63 溢出
])
def test_div_truncating_semantics(a, b, expected_q):
    r = run_arith(f"PUSH8 {a}\nPUSH8 {b}\nDIV\nSTOP")
    if expected_q is None:
        assert not r.ok
        assert r.error_category == str(Failure.INTEGER_OVERFLOW)
    else:
        assert r.ok, r.error_category
        assert r.return_value == expected_q
        ref = reference_div(a, b)
        assert ref == expected_q


@pytest.mark.parametrize("a,b,expected", [
    (43, 6, 1),
    (-43, 6, -1),
    (43, -6, 1),
    (-43, -6, -1),
])
def test_mod_remainder_sign_follows_dividend(a, b, expected):
    r = run_arith(f"PUSH8 {a}\nPUSH8 {b}\nMOD\nSTOP")
    assert r.ok, r.error_category
    assert r.return_value == expected
    ref = reference_truncating_divmod(a, b)
    assert ref[1] == expected


def test_mod_by_zero():
    r = run_arith("PUSH8 1\nPUSH8 0\nMOD\nSTOP")
    assert not r.ok
    assert r.error_category == str(Failure.DIV_BY_ZERO)


@pytest.mark.parametrize("a,b,n,expected", [
    (5, 6, 7, 4),
    (2**40, 2**40, 1_000_000_007, (2**40 * 2) % 1_000_000_007),
    (INT64_MAX, INT64_MAX, 7, (INT64_MAX * 2) % 7),  # 全精度中间值
])
def test_addmod_arbitrary_precision(a, b, n, expected):
    r = run_arith(f"PUSH8 {a}\nPUSH8 {b}\nPUSH8 {n}\nADDMOD\nSTOP")
    assert r.ok, r.error_category
    assert r.return_value == expected
    assert expected == (a + b) % n


def test_addmod_modulus_must_be_positive_i64():
    # 栈上操作数都是 64 位有符号数；最大正模数为 2^63-1，结果必然小于模数，
    # 因此合法操作数下 ADDMOD 不会溢出。非正模数（含 0）统一归为除零类。
    r = run_arith(f"PUSH8 1\nPUSH8 0\nPUSH8 {-(1 << 63)}\nADDMOD\nSTOP")
    assert not r.ok
    assert r.error_category == str(Failure.DIV_BY_ZERO)


def test_addmod_with_max_i64_modulus():
    n = 2**63 - 1
    r = run_arith(f"PUSH8 {n}\nPUSH8 {n}\nPUSH8 {n}\nADDMOD\nSTOP")
    assert r.ok, r.error_category
    # 2*(2^63-1) mod (2^63-1) = 0（全精度中间值）
    assert r.return_value == 0


@pytest.mark.parametrize("n", [0, -3])
def test_addmod_nonpositive_divisor_is_div_by_zero(n):
    r = run_arith(f"PUSH8 1\nPUSH8 2\nPUSH8 {n}\nADDMOD\nSTOP")
    assert not r.ok
    assert r.error_category == str(Failure.DIV_BY_ZERO)


def test_mulmod_giant_intermediate():
    # (2^40)^2 远超 64 位，但对 1e9+7 取模有确定结果
    a = 2**40
    r = run_arith(f"PUSH8 {a}\nPUSH8 {a}\nPUSH8 1000000007\nMULMOD\nSTOP")
    assert r.ok, r.error_category
    assert r.return_value == (a * a) % 1_000_000_007


@pytest.mark.parametrize("text,expected", [
    ("PUSH8 3\nPUSH8 5\nLT\nSTOP", 1),    # 3 < 5
    ("PUSH8 5\nPUSH8 3\nLT\nSTOP", 0),
    ("PUSH8 5\nPUSH8 3\nGT\nSTOP", 1),
    ("PUSH8 5\nPUSH8 5\nEQ\nSTOP", 1),
    ("PUSH8 0\nISZERO\nSTOP", 1),
    ("PUSH8 9\nISZERO\nSTOP", 0),
])
def test_comparisons(text, expected):
    r = run_arith(text)
    assert r.ok
    assert r.return_value == expected


def test_push1_is_zero_extended():
    r = run_arith("PUSH1 255\nPUSH1 1\nADD\nSTOP")
    assert r.ok
    assert r.return_value == 256


def test_push8_signed_boundary_roundtrip():
    r = run_arith(f"PUSH8 {INT64_MIN}\nPUSH8 0\nADD\nSTOP")
    assert r.ok
    assert r.return_value == INT64_MIN
