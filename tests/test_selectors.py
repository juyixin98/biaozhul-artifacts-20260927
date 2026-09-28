"""Selector golden vectors against the mature eth_utils Keccak oracle."""
from __future__ import annotations

import pytest

eth_utils = pytest.importorskip("eth_utils")

from abibackend import abi  # noqa: E402
from abibackend.crypto import keccak256  # noqa: E402


# Hand-fixed, well-known selectors (Solidity ABI spec / common signatures).
KNOWN = {
    ("transfer", ["address", "uint256"]): "a9059cbb",
    ("approve", ["address", "uint256"]): "095ea7b3",
    ("balanceOf", ["address"]): "70a08231",
    ("transferFrom", ["address", "address", "uint256"]): "23b872dd",
    ("totalSupply", []): "18160ddd",
    ("allowance", ["address", "address"]): "dd62ed3e",
}


@pytest.mark.parametrize("name_types,expected", [(k, v) for k, v in KNOWN.items()])
def test_known_selectors(name_types, expected, log):
    name, types = name_types
    sig = name + "(" + ",".join(types) + ")"
    got = abi.function_selector(name, types).hex()
    # Independent oracle: eth_utils.keccak on the same ASCII signature.
    oracle = eth_utils.keccak(sig.encode("ascii")).hex()[:8]
    log("selector", "info", signature=sig, ours=got, eth_utils=oracle, known=expected)
    assert got == expected
    assert got == oracle
    assert keccak256(sig.encode("ascii")).hex()[:8] == expected


def test_keccak_known_empty_vector():
    # keccak256("") published digest (distinct from NIST sha3-256)
    assert keccak256(b"").hex() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )


def test_encode_call_prefixes_selector():
    blob = abi.encode_call("transfer", ["address", "uint256"], [0xAB, 1])
    assert blob[:4].hex() == "a9059cbb"
    args = abi.decode(["address", "uint256"], blob[4:])
    assert args == (0xAB, 1)
