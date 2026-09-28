"""Integer sign extension / fill rules and value-range validation."""
from __future__ import annotations

import pytest

from abibackend import abi
from abibackend.abi import ValueOutOfRange
from abibackend.abi.encoder import encode_value
from abibackend.abi.types import parse_type


def test_uint_bounds():
    assert encode_value(parse_type("uint256"), 0) == b"\x00" * 32
    assert encode_value(parse_type("uint256"), 2**256 - 1) == b"\xff" * 32
    with pytest.raises(ValueOutOfRange):
        encode_value(parse_type("uint256"), 2**256)
    with pytest.raises(ValueOutOfRange):
        encode_value(parse_type("uint256"), -1)


def test_int256_extremes_and_extension():
    assert encode_value(parse_type("int256"), -1) == b"\xff" * 32
    assert encode_value(parse_type("int256"), 1)[0] == 0
    assert encode_value(parse_type("int256"), -(2**255)) == b"\x80" + b"\x00" * 31
    assert encode_value(parse_type("int256"), 2**255 - 1) == b"\x7f" + b"\xff" * 31
    with pytest.raises(ValueOutOfRange):
        encode_value(parse_type("int256"), 2**255)
    with pytest.raises(ValueOutOfRange):
        encode_value(parse_type("int256"), -(2**255) - 1)


def test_narrow_int_fill():
    # int8 -1 must fill the whole 32-byte word with ff
    assert encode_value(parse_type("int8"), -1) == b"\xff" * 32
    # uint8 1 must be zero-padded on the left
    assert encode_value(parse_type("uint8"), 1) == b"\x00" * 31 + b"\x01"


def test_bool_is_not_int():
    with pytest.raises(ValueOutOfRange):
        encode_value(parse_type("uint256"), True)


def test_address_forms():
    assert encode_value(parse_type("address"), 255) == b"\x00" * 31 + b"\xff"
    assert encode_value(parse_type("address"), "0x" + "ff" * 20) == b"\x00" * 12 + b"\xff" * 20
    with pytest.raises(ValueOutOfRange):
        encode_value(parse_type("address"), 2**160)
    with pytest.raises(ValueOutOfRange):
        encode_value(parse_type("address"), "0x12")


def test_bytesN_fill_and_length():
    assert encode_value(parse_type("bytes4"), b"\xde\xad\xbe\xef")[4:] == b"\x00" * 28
    with pytest.raises(ValueOutOfRange):
        encode_value(parse_type("bytes4"), b"\xab")


@pytest.mark.parametrize("spec", ["uint7", "uint9", "uint0", "uint264", "bytes0", "bytes33", "int", "uint"])
def test_invalid_widths(spec):
    with pytest.raises((abi.InvalidType, abi.UnsupportedType)):
        parse_type(spec)


def test_type_canonical_strings():
    assert parse_type("(uint256,bytes[])").canonical() == "(uint256,bytes[])"
    assert parse_type("uint256[][3]").canonical() == "uint256[][3]"
    assert parse_type(["uint256", "bytes"]).canonical() == "(uint256,bytes)"
