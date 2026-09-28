"""Domain models: transactions and blocks, plus the signing envelope.

These helpers are the *single* place that defines wire membership, so the
ingestion kernel, the fixture builder and the independent test oracle all
agree on what a signature covers:

* transaction signing payload::

      canonical_json({type, nonce, sender, recipient, amount, fee, fee_recipient})

* transaction id (``txid``) = sha256 hex of that payload;
* block identity hash and PoW are defined in :mod:`crypto.hashing`.
"""
from __future__ import annotations

from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..crypto.encoding import canonical_json
from ..crypto.hashing import (
    block_identity_hash,
    merkle_root,
    sha256_hex,
)
from ..crypto.keys import (
    address_from_private_key,
    public_key_bytes,
    sign,
)

TX_TRANSFER = "transfer"
TX_MINT = "mint"
KNOWN_TX_TYPES = (TX_TRANSFER, TX_MINT)

TX_PAYLOAD_FIELDS = (
    "type",
    "nonce",
    "sender",
    "recipient",
    "amount",
    "fee",
    "fee_recipient",
)


def tx_signing_payload(tx: dict) -> bytes:
    return canonical_json({field: tx[field] for field in TX_PAYLOAD_FIELDS})


def txid(tx: dict) -> str:
    return sha256_hex(tx_signing_payload(tx))


def make_transfer(
    *,
    signer: Ed25519PrivateKey,
    nonce: int,
    recipient: str,
    amount: int,
    fee: int = 0,
    fee_recipient: str,
) -> dict[str, Any]:
    """Build and sign a transfer transaction."""
    sender = address_from_private_key(signer)
    body: dict[str, Any] = {
        "type": TX_TRANSFER,
        "nonce": nonce,
        "sender": sender,
        "recipient": recipient,
        "amount": str(amount),
        "fee": str(fee),
        "fee_recipient": fee_recipient,
    }
    signing = canonical_json(body)
    body["txid"] = sha256_hex(signing)
    body["pubkey"] = public_key_bytes(signer).hex()
    body["signature"] = sign(signer, signing).hex()
    return body


def make_mint(
    *,
    signer: Ed25519PrivateKey,
    recipient: str,
    amount: int,
) -> dict[str, Any]:
    """Build and sign a mint transaction (valid only inside the genesis block).

    Mints use the signer's own address as ``sender`` and nonce 0; the kernel
    accepts them only at height 0.
    """
    sender = address_from_private_key(signer)
    body: dict[str, Any] = {
        "type": TX_MINT,
        "nonce": 0,
        "sender": sender,
        "recipient": recipient,
        "amount": str(amount),
        "fee": "0",
        "fee_recipient": sender,
    }
    signing = canonical_json(body)
    body["txid"] = sha256_hex(signing)
    body["pubkey"] = public_key_bytes(signer).hex()
    body["signature"] = sign(signer, signing).hex()
    return body


def make_block(
    *,
    height: int,
    parent: str,
    producer: Ed25519PrivateKey,
    transactions: list[dict],
    difficulty: int,
    timestamp: str,
    version: int = 1,
    nonce: int,
) -> dict[str, Any]:
    """Assemble a sealed block dictionary (already mined and signed).

    Mining (searching ``nonce``) lives in the fixture builder so that the
    kernel never mutates submitted blocks.
    """
    root = merkle_root([tx["txid"] for tx in transactions])
    header = {
        "version": version,
        "height": height,
        "parent": parent,
        "merkle_root": root,
        "difficulty": difficulty,
        "timestamp": timestamp,
        "producer": public_key_bytes(producer).hex(),
        "nonce": str(nonce),
    }
    block: dict[str, Any] = dict(header)
    identity = block_identity_hash(header)
    block["pow_signature"] = sign(producer, bytes.fromhex(identity)).hex()
    block["transactions"] = transactions
    return block
