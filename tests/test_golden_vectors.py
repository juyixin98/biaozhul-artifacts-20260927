"""Golden-vector cross-check against the mature ``eth_abi`` library.

IMPORTANT: eth_abi is an *independent oracle* — the reference encodings come
from that library (plus a handful of hand-fixed canonical bytes), never from the
decoder under test. We assert both:
  1. our encode(...) == eth_abi.encode(...) byte-for-byte;
  2. eth_abi.decode(our_blob) round-trips and our decode(eth_abi_blob) matches.

Coverage demanded by the spec: nested dynamic arrays, empty bytes/string,
negative integers, non-canonical padding, and rich nesting of arrays/tuples.
"""
from __future__ import annotations

import pytest

eth_abi = pytest.importorskip("eth_abi")
eth_utils = pytest.importorskip("eth_utils")

from abibackend import abi as ours  # noqa: E402

# --------------------------------------------------------------------------- #
# Hand-fixed canonical vectors (not produced by the code under test).
# These document head/tail + relative-offset behaviour explicitly.
# --------------------------------------------------------------------------- #
HAND_FIXED = [
    # (types, values, expected_hex)
    (
        ["uint256"],
        [1],
        "0000000000000000000000000000000000000000000000000000000000000001",
    ),
    (
        ["int256"],
        [-1],
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    ),
    (
        ["int8"],
        [-1],
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    ),
    (
        ["int8"],
        [-128],
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff80",
    ),
    (
        ["uint8"],
        [255],
        "00000000000000000000000000000000000000000000000000000000000000ff",
    ),
    (
        ["bool"],
        [True],
        "0000000000000000000000000000000000000000000000000000000000000001",
    ),
    (
        ["address"],
        [0xFF],
        "00000000000000000000000000000000000000000000000000000000000000ff",
    ),
    (
        ["bytes4"],
        [b"\xde\xad\xbe\xef"],
        "deadbeef00000000000000000000000000000000000000000000000000000000",
    ),
    (
        ["bytes"],
        [b""],
        # head: offset 0x20; body: length 0
        "0000000000000000000000000000000000000000000000000000000000000020"
        "0000000000000000000000000000000000000000000000000000000000000000",
    ),
    (
        ["string"],
        [""],
        "0000000000000000000000000000000000000000000000000000000000000020"
        "0000000000000000000000000000000000000000000000000000000000000000",
    ),
    (
        ["bytes"],
        [b"\xca\xfe"],
        # off=0x20, len=2, data + 30 zero pad bytes
        "0000000000000000000000000000000000000000000000000000000000000020"
        "0000000000000000000000000000000000000000000000000000000000000002"
        "cafe000000000000000000000000000000000000000000000000000000000000",
    ),
    (
        ["string"],
        ["abc"],
        "0000000000000000000000000000000000000000000000000000000000000020"
        "0000000000000000000000000000000000000000000000000000000000000003"
        "6162630000000000000000000000000000000000000000000000000000000000",
    ),
    (
        ["uint256[]"],
        [[]],
        # head: offset 0x20; body: length 0
        "0000000000000000000000000000000000000000000000000000000000000020"
        "0000000000000000000000000000000000000000000000000000000000000000",
    ),
]


def _eth_types(types):
    return tuple(types)


def _eth_blob(types, values):
    # eth_abi.encode(types_tuple, args)
    return eth_abi.encode(_eth_types(types), values)


def _eth_decode(types, blob):
    return eth_abi.decode(_eth_types(types), blob)


@pytest.mark.parametrize("types,values,expected", HAND_FIXED)
def test_hand_fixed_vectors(types, values, expected, log):
    blob = ours.encode(types, values)
    log("encode-hand-fixed", "info", types=types, values=repr(values), got=blob.hex())
    assert blob.hex() == expected
    # decode back
    decoded = ours.decode(types, blob)
    assert _norm(types, decoded) == _norm(types, tuple(values))


# --------------------------------------------------------------------------- #
# Property-style cases cross-checked against eth_abi (independent oracle).
# --------------------------------------------------------------------------- #
CROSS_CASES = [
    (["uint256", "uint8"], (123456, 255)),
    (["int256"], (-1,)),
    (["int256"], (-(2**255),)),
    (["int256"], (2**255 - 1,)),
    (["int128"], (-123987654321,)),
    (["int8", "uint8", "int16"], (-5, 5, -300)),
    (["bool", "bool"], (True, False)),
    (["address"], (0x0123456789012345678901234567890123456789,)),
    (["bytes32"], (b"\x00" * 31 + b"\x01",)),
    (["bytes1"], (b"\xff",)),
    (["bytes", "bytes"], (b"", b"")),
    (["bytes", "bytes"], (b"", b"\x01\x02")),
    (["bytes", "bytes"], (b"hello", b"")),
    (["string", "string"], ("", "")),
    (["string"], ("",)),
    (["string"], ("héllo 世界",)),  # multibyte utf-8
    (["uint256[]"], ([1, 2, 3],)),
    (["uint256[]"], ([],)),
    (["int256[]"], ([-1, -2, 0, 2**255 - 1],)),
    (["bytes[]"], ([b"a", b"", b"longer-bytes"],)),
    (["string[]"], (["", "x", "yy"],)),
    (["uint256[3]"], ((1, 2, 3),)),
    (["uint256[3]"], ([1, 2, 3],)),
    (["bytes[2]"], ((b"a", b"bb"),)),
    (["(uint256,uint256)"], ((7, 8),)),
    (["(uint256,bytes)"], ((9, b"tail"),)),
    (["(int256,string)"], ((-42, "neg"),)),
    # nested dynamic arrays: dynamic array of dynamic arrays
    (["uint256[][]"], ([[1], [2, 3], []],)),
    (["uint256[][]"], ([[], [], []],)),
    (["bytes[][]"], ([[b"", b"x"], [b"yy"], []],)),
    # fixed array of dynamic arrays
    (["uint256[][2]"], (([1, 2], []),)),
    # dynamic array of tuples containing dynamic members
    (["(uint256,bytes)[]"], ([(1, b"a"), (2, b"")],)),
    (["(int256,string)[]"], ([(-1, ""), (5, "z")],)),
    # tuple containing a dynamic array AND a dynamic bytes (two dynamic siblings)
    (["(uint256[],bytes)"], (([10, 20], b"Q"),)),
    (["(bytes,uint256[],string)"], ((b"Q", [1, 2, 3], "s"),)),
    # deeply nested tuple/array mix
    (["(uint256,(bytes,string)[])"], ((3, [(b"k", "v"), (b"", "")]),)),
    (["(address,uint256[],bool)"], ((0xAB, [1, 2], True),)),
]


def _norm(types, values):
    """Normalize eth_abi outputs (bytes addresses) to our representation."""
    out = []
    for t, v in zip(types, values):
        out.append(_norm_one(ours.parse_type(t), v))
    return tuple(out)


def _norm_one(header, v):
    from abibackend.abi.types import (
        AddressType,
        DynamicArrayType,
        FixedArrayType,
        FixedBytesType,
        BytesType,
        TupleType,
    )

    if isinstance(header, AddressType):
        if isinstance(v, bytes):
            return int.from_bytes(v, "big")
        return int(v)
    if isinstance(header, (BytesType, FixedBytesType)):
        return bytes(v)
    if isinstance(header, TupleType):
        return tuple(_norm_one(h, x) for h, x in zip(header.components, v))
    if isinstance(header, (DynamicArrayType, FixedArrayType)):
        return tuple(_norm_one(header.element, x) for x in v)
    return v


@pytest.mark.parametrize("types,values", CROSS_CASES)
def test_encode_matches_ethabi(types, values, log):
    expected = _eth_blob(types, list(values))
    got = ours.encode(types, list(values))
    log("cross-encode", "info", types=types, values=repr(values),
        eth_abi=expected.hex(), ours=got.hex())
    assert got == expected, f"\n eth_abi={expected.hex()}\n ours  ={got.hex()}"


@pytest.mark.parametrize("types,values", CROSS_CASES)
def test_our_decode_of_ethabi_blob(types, values, log):
    eth_blob = _eth_blob(types, list(values))
    decoded = ours.decode(types, eth_blob)
    want = _norm(types, _eth_decode(types, eth_blob))
    log("cross-decode", "info", types=types, blob=eth_blob.hex(), decoded=repr(decoded))
    assert decoded == want


@pytest.mark.parametrize("types,values", CROSS_CASES)
def test_ethabi_decodes_our_blob(types, values):
    blob = ours.encode(types, list(values))
    # Independent oracle must accept our encoding.
    theirs = _eth_decode(types, blob)
    assert _norm(types, theirs) == _norm(types, tuple(values))


def test_empty_tuple_roundtrip():
    blob = ours.encode([], [])
    assert blob == b""
    assert ours.decode([], blob) == ()
