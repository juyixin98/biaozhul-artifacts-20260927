"""Reusable synthetic fixtures.

Everything here is *locally generated*: deterministic secp256k1 keys from a
fixed seed (no production accounts), real signed transactions, and block
payloads with explicit per-transaction executed gas. Fixtures can be exported
to JSON so the exact same bytes can be replayed by the CLI, the API and tests.

Deterministic identities
------------------------
``fixture_account("alice")`` always yields the same key/address across runs
because the key is derived from ``keccak256("basefee-fixture:" + label)``.

Load profiles
-------------
Helpers build raw signed transaction bytes plus a declared ``gas_used`` so we
can target a precise block fill ratio (empty / target / full / arbitrary gas),
which drives every branch of the base-fee recurrence.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from coincurve import PrivateKey

from ..config import DEFAULT_CHAIN_ID, DEFAULT_GAS_LIMIT, TX_TYPE_EIP1559, TX_TYPE_LEGACY
from ..encoding.crypto import address_from_private, generate_private_key
from ..encoding.transaction import Transaction

_FIXTURE_TAG = b"basefee-fixture:"


def fixture_key(label: str) -> PrivateKey:
    """Deterministic private key for a named synthetic identity."""
    return generate_private_key(_FIXTURE_TAG + label.encode())


def fixture_account(label: str) -> dict:
    key = fixture_key(label)
    addr = address_from_private(key)
    return {"label": label, "key": key, "address": addr,
            "address_hex": "0x" + addr.hex()}


@dataclass
class FixtureWallet:
    label: str
    key: PrivateKey
    address: bytes

    @property
    def address_hex(self) -> str:
        return "0x" + self.address.hex()


def wallet(label: str) -> FixtureWallet:
    acc = fixture_account(label)
    return FixtureWallet(label=label, key=acc["key"], address=acc["address"])


# -- transaction builders ---------------------------------------------------

def sign_eip1559_tx(
    signer: FixtureWallet, nonce: int, *,
    max_fee_per_gas: int, max_priority_fee_per_gas: int,
    gas_limit: int = 21_000, value: int = 0, data: bytes = b"",
    to: bytes | None = None, chain_id: int = DEFAULT_CHAIN_ID,
) -> tuple[Transaction, bytes]:
    """Sign a type-2 tx; return (Transaction, raw bytes)."""
    if to is None:
        to = b"\x00" * 20  # synthetic burn placeholder recipient
    tx = Transaction(
        type=TX_TYPE_EIP1559, nonce=nonce, gas_limit=gas_limit, to=to,
        value=value, data=data, chain_id=chain_id,
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
    )
    tx.sign(signer.key)
    return tx, tx.encoded()


def sign_legacy_tx(
    signer: FixtureWallet, nonce: int, *, gas_price: int,
    gas_limit: int = 21_000, value: int = 0, data: bytes = b"",
    to: bytes | None = None, chain_id: int = DEFAULT_CHAIN_ID,
) -> tuple[Transaction, bytes]:
    if to is None:
        to = b"\x00" * 20
    tx = Transaction(
        type=TX_TYPE_LEGACY, nonce=nonce, gas_limit=gas_limit, to=to,
        value=value, data=data, chain_id=chain_id, gas_price=gas_price,
    )
    tx.sign(signer.key)
    return tx, tx.encoded()


# -- load profiles ----------------------------------------------------------

def _raw_hex(raw: bytes) -> str:
    return "0x" + raw.hex()


def target_fill_block(label: str, number: int, gas_target: int,
                      fee_cap: int, priority: int, *, tx_type: int =
                      TX_TYPE_EIP1559, gas_price: int | None = None,
                      signer: FixtureWallet | None = None,
                      nonce_start: int = 0) -> dict:
    """One tx whose executed gas exactly equals ``gas_target`` (target load)."""
    w = signer or wallet(label)
    # A plain transfer is 21000; to reach an arbitrary gas target we widen
    # gas_limit/executed gas (synthetic execution result).
    gl = max(gas_target, 21_000)
    if tx_type == TX_TYPE_LEGACY:
        _, raw = sign_legacy_tx(w, nonce_start, gas_price=gas_price or fee_cap,
                                gas_limit=gl)
    else:
        _, raw = sign_eip1559_tx(w, nonce_start, max_fee_per_gas=fee_cap,
                                 max_priority_fee_per_gas=priority,
                                 gas_limit=gl)
    return {"number": number, "raw_transactions": [_raw_hex(raw)],
            "tx_gas_used": [gas_target]}


def empty_block(number: int) -> dict:
    return {"number": number, "raw_transactions": [], "tx_gas_used": []}


def full_block(label: str, number: int, gas_limit: int, fee_cap: int,
               priority: int, *, signer: FixtureWallet | None = None,
               nonce_start: int = 0) -> dict:
    w = signer or wallet(label)
    gl = max(gas_limit, 21_000)
    _, raw = sign_eip1559_tx(w, nonce_start, max_fee_per_gas=fee_cap,
                             max_priority_fee_per_gas=priority, gas_limit=gl)
    return {"number": number, "raw_transactions": [_raw_hex(raw)],
            "tx_gas_used": [gas_limit]}


def arbitrary_fill_block(label: str, number: int, gas_used: int,
                         fee_cap: int, priority: int, *,
                         signer: FixtureWallet | None = None,
                         nonce_start: int = 0) -> dict:
    w = signer or wallet(label)
    gl = max(gas_used, 21_000)
    _, raw = sign_eip1559_tx(w, nonce_start, max_fee_per_gas=fee_cap,
                             max_priority_fee_per_gas=priority, gas_limit=gl)
    return {"number": number, "raw_transactions": [_raw_hex(raw)],
            "tx_gas_used": [gas_used]}


# -- scenario export/import -------------------------------------------------

def write_fixture(path: str, *, genesis_base_fee: int,
                  gas_limit: int, alloc: dict[str, int],
                  blocks: list[dict]) -> None:
    payload = {
        "schema": "basefee-fixture/v1",
        "chain_id": DEFAULT_CHAIN_ID,
        "genesis_base_fee": genesis_base_fee,
        "gas_limit": gas_limit,
        "alloc": alloc,
        "blocks": blocks,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def read_fixture(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# -- canonical scenario -----------------------------------------------------

def canonical_scenario(*, genesis_base_fee: int = 1_000_000_000,
                       gas_limit: int = DEFAULT_GAS_LIMIT) -> dict:
    """Target -> full -> full -> empty -> empty -> target multi-block chain.

    Exercises up (min-increment on tiny fees is separately vector tested),
    down to zero-floor and flat directions; includes a legacy tx for envelope
    coverage; and is fully funded so every transaction validates.
    """
    target = gas_limit // 2
    funder = wallet("funder")
    receiver = wallet("receiver")
    alloc = {funder.address_hex: 10 ** 30}
    huge = 10 ** 18  # 1 ETH per gas cap; always above any base fee reached
    prio = 10 ** 9

    blocks = [
        # 1: target load -> flat
        target_fill_block("funder", 1, target, huge, prio,
                          signer=funder, nonce_start=0),
        # 2: full -> up
        full_block("funder", 2, gas_limit, huge, prio,
                   signer=funder, nonce_start=1),
        # 3: full -> up again
        full_block("funder", 3, gas_limit, huge, prio,
                   signer=funder, nonce_start=2),
        # 4: empty -> down
        empty_block(4),
        # 5: empty -> down further
        empty_block(5),
        # 6: arbitrary 3/4 fill -> up
        arbitrary_fill_block("funder", 6, gas_limit * 3 // 4, huge, prio,
                             signer=funder, nonce_start=3),
        # 7: legacy transaction at target load
        target_fill_block("funder", 7, target, gas_price=huge, fee_cap=huge,
                          priority=prio, tx_type=TX_TYPE_LEGACY,
                          signer=funder, nonce_start=4),
    ]
    return {"genesis_base_fee": genesis_base_fee, "gas_limit": gas_limit,
            "alloc": alloc, "blocks": blocks}


FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "fixtures")
