"""函数选择器与 calldata 测试。"""

from __future__ import annotations

import pytest

from app.abi import decode_call, encode_call, function_selector
from app.abi.errors import UnsupportedTypeError
from app.kernel import get_registry


def test_known_erc20_selectors(golden):
    """钉死的知名选择器必须逐字节匹配（Keccak 正确性的强信号）。"""
    for row in golden["selectors"]:
        got = function_selector(row["signature"]).hex()
        assert got == row["selector_hex"], (
            f"{row['signature']}: {got} != {row['selector_hex']}"
        )


def test_selector_is_four_bytes():
    assert len(function_selector("f()")) == 4


def test_encode_decode_call_roundtrip():
    sig = "transfer(bytes20,bytes20,uint256)"
    args = (b"A" * 20, b"B" * 20, 100)
    data = encode_call(sig, args)
    assert data[:4] == function_selector(sig)
    reg = {function_selector(sig): (sig, ("bytes20", "bytes20", "uint256"))}
    got_sig, got_args = decode_call(data, reg)
    assert got_sig == sig
    assert got_args == args


def test_unknown_selector_rejected():
    with pytest.raises(UnsupportedTypeError):
        decode_call(b"\xde\xad\xbe\xef" + b"\x00" * 32, get_registry())


def test_short_calldata_rejected():
    from app.abi.errors import InvalidTypeError

    with pytest.raises(InvalidTypeError):
        decode_call(b"\x00\x00", get_registry())


def test_kernel_registry_selectors_resolve():
    reg = get_registry()
    # 注册表中的选择器都能反查签名
    for sel, (sig, types) in reg.items():
        assert len(sel) == 4 and "(" in sig
