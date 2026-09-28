"""类型系统与编码值校验的单元测试。"""

from __future__ import annotations

import pytest

from app.abi import decode, encode
from app.abi.errors import (
    ABIValueError,
    InvalidTypeError,
    LengthMismatchError,
    UnsupportedTypeError,
)


def test_int_widths_accepted():
    for bits in range(8, 257, 8):
        enc = encode([f"uint{bits}"], (1,))
        assert decode([f"uint{bits}"], enc) == (1,)
        enc = encode([f"int{bits}"], (-1,))
        assert decode([f"int{bits}"], enc) == (-1,)


@pytest.mark.parametrize("bad", ["uint0", "uint7", "uint9", "uint264", "int300", "bytes0", "bytes33"])
def test_invalid_widths(bad):
    with pytest.raises(InvalidTypeError):
        encode([bad], (0,))


def test_unsupported_elementary():
    with pytest.raises(UnsupportedTypeError):
        encode(["address"], (b"\x00" * 20,))


def test_uint_rejects_negative():
    with pytest.raises(ABIValueError):
        encode(["uint256"], (-1,))


def test_int_range_enforced():
    with pytest.raises(ABIValueError):
        encode(["int8"], (128,))
    with pytest.raises(ABIValueError):
        encode(["int8"], (-129,))


def test_bytesN_length_enforced():
    with pytest.raises(LengthMismatchError):
        encode(["bytes4"], (b"abc",))
    with pytest.raises(LengthMismatchError):
        encode(["bytes4"], (b"abcde",))


def test_fixed_array_length_enforced():
    with pytest.raises(LengthMismatchError):
        encode(["uint256[3]"], ((1, 2),))


def test_value_type_mismatch():
    with pytest.raises(ABIValueError):
        encode(["uint256"], ("1",))
    with pytest.raises(ABIValueError):
        encode(["bytes"], ("not bytes",))
    with pytest.raises(ABIValueError):
        encode(["string"], (b"not str",))


def test_top_level_arity_mismatch():
    with pytest.raises(LengthMismatchError):
        encode(["uint256", "uint256"], (1,))


def test_bool_rejected_as_int():
    # bool 是 int 子类，必须显式拒绝以免歧义
    with pytest.raises(ABIValueError):
        encode(["uint256"], (True,))


def test_empty_type_sequence():
    assert encode([], ()) == b""
    assert decode([], b"") == ()
