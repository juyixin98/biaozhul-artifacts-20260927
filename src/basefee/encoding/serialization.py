"""Canonical serialization, signing digests and identity hashes.

Synthetic domain choice
-----------------------
This is *not* wired to a real chain. To stay dependency-light and fully local,
identity derivation uses SHA-256 over RLP instead of keccak-256:

* address  = last 20 bytes of SHA-256(public_key_x || public_key_y)
* tx digest (signed) = SHA-256(RLP(EIP-1559-style transaction fields))
* tx hash           = SHA-256(RLP(the same fields + r || s || v))
* block hash        = SHA-256(RLP(header fields || list-of-tx-hashes))

The RLP field *layout* mirrors the EIP-1559 type-2 transaction; only the hash
function differs, and it differs consistently and explicitly (see
``docs/SEMANTICS.md``). All integers are RLP scalars (minimal big-endian).
"""

from __future__ import annotations

import hashlib
from typing import Sequence

from .rlp import encode
from .hexutil import int_to_minimal_bytes

ADDRESS_BYTES = 20


def _scalar(value: int) -> bytes:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("expected integer scalar")
    return int_to_minimal_bytes(value)


def address_from_pubkey(pubkey_xy: bytes) -> str:
    """Derive the synthetic 20-byte hex address from a 64-byte raw public key."""
    if len(pubkey_xy) != 64:
        raise ValueError("public key must be 64 bytes (x||y)")
    return "0x" + hashlib.sha256(pubkey_xy).digest()[-ADDRESS_BYTES:].hex()


def tx_signing_payload(
    *,
    chain_id: int,
    nonce: int,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
    gas_limit: int,
    to: bytes,
    value: int,
    data: bytes,
) -> bytes:
    fields = [
        _scalar(chain_id),
        _scalar(nonce),
        _scalar(max_fee_per_gas),
        _scalar(max_priority_fee_per_gas),
        _scalar(gas_limit),
        to,
        _scalar(value),
        data,
    ]
    return encode(fields)


def tx_digest(*, chain_id: int, nonce: int, max_fee_per_gas: int,
              max_priority_fee_per_gas: int, gas_limit: int, to: bytes,
              value: int, data: bytes) -> bytes:
    return hashlib.sha256(
        tx_signing_payload(
            chain_id=chain_id,
            nonce=nonce,
            max_fee_per_gas=max_fee_per_gas,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
            gas_limit=gas_limit,
            to=to,
            value=value,
            data=data,
        )
    ).digest()


def encode_signed_fields(*, chain_id, nonce, max_fee_per_gas, max_priority_fee_per_gas,
                         gas_limit, to, value, data, r, s, v) -> bytes:
    fields = [
        _scalar(chain_id),
        _scalar(nonce),
        _scalar(max_fee_per_gas),
        _scalar(max_priority_fee_per_gas),
        _scalar(gas_limit),
        to,
        _scalar(value),
        data,
        _scalar(r),
        _scalar(s),
        _scalar(v),
    ]
    return encode(fields)


def signed_tx_hash(*, chain_id: int, nonce: int, max_fee_per_gas: int,
                   max_priority_fee_per_gas: int, gas_limit: int, to: bytes,
                   value: int, data: bytes, r: int, s: int, v: int) -> str:
    raw = encode_signed_fields(
        chain_id=chain_id, nonce=nonce, max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas, gas_limit=gas_limit,
        to=to, value=value, data=data, r=r, s=s, v=v,
    )
    return "0x" + hashlib.sha256(raw).hexdigest()


def block_header_hash(*, parent_hash: bytes, number: int, base_fee_per_gas: int,
                      gas_limit: int, gas_used: int, tx_hashes: Sequence[bytes]) -> bytes:
    fields = [
        parent_hash,
        _scalar(number),
        _scalar(base_fee_per_gas),
        _scalar(gas_limit),
        _scalar(gas_used),
        list(tx_hashes),
    ]
    return hashlib.sha256(encode(fields)).digest()
