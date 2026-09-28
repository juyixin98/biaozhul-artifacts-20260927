"""Shared pytest fixtures.

* Keys/transactions are signed with the independent oracle (direct ecdsa usage),
  never with the project's own signing wrapper, so tests feed externally-produced
  signatures into the kernel.
* The committed chain fixture is re-generated on the fly by the same builder
  logic only when missing; normally tests use the committed JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from reference import oracle as O  # noqa: E402


@pytest.fixture(scope="session")
def oracle():
    return O


def _key_pair(oracle, seed):
    sk = oracle.oracle_key(seed)
    return sk, oracle.oracle_address(oracle.oracle_pub_raw(sk))


@pytest.fixture
def keys(oracle):
    return {
        "alice": _key_pair(oracle, 1),
        "bob": _key_pair(oracle, 2),
        "carol": _key_pair(oracle, 3),
    }


@pytest.fixture
def hand_vectors():
    return json.loads((ROOT / "fixtures" / "hand_vectors.json").read_text())


@pytest.fixture
def chain_fixture():
    return json.loads((ROOT / "fixtures" / "chain_fixture.json").read_text())


def sign_with_oracle(oracle, sk, **kwargs):
    """Return a structured wire tx dict signed via the independent oracle."""
    to = kwargs["to"]
    sig = oracle.oracle_sign(
        sk, nonce=kwargs.get("nonce", 0),
        max_fee=kwargs["max_fee"], max_tip=kwargs["max_tip"],
        gas_limit=kwargs.get("gas", 21000),
        to=bytes.fromhex(to[2:]), value=kwargs.get("value", 0),
        data=kwargs.get("data", b""),
    )
    return {
        "chain_id": oracle.CHAIN_ID,
        "nonce": kwargs.get("nonce", 0),
        "max_fee_per_gas": str(kwargs["max_fee"]),
        "max_priority_fee_per_gas": str(kwargs["max_tip"]),
        "gas_limit": kwargs.get("gas", 21000),
        "to": to,
        "value": str(kwargs.get("value", 0)),
        "data": "0x" + kwargs.get("data", b"").hex(),
        "signature": {"r": str(sig["r"]), "s": str(sig["s"]), "v": sig["v"]},
    }


@pytest.fixture
def sign(oracle):
    def _f(sk, **kwargs):
        return sign_with_oracle(oracle, sk, **kwargs)
    return _f
