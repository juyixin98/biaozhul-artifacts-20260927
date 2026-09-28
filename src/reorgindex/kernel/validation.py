"""Stateless block/transaction verification: encoding, hashes, PoW, signatures.

Everything here can be checked without knowing the chain tip.  Stateful rules
(nonces, balances, replay, parent linkage) live in :mod:`kernel.state` and the
engine.
"""
from __future__ import annotations

import re

from ..crypto.encoding import canonical_json
from ..crypto.hashing import (
    block_identity_hash,
    merkle_root,
    pow_satisfied,
)
from ..crypto.keys import (
    address_from_pubkey,
    public_key_from_hex,
    verify,
)
from .errors import IngestionError, RejectReason
from .models import TX_MINT, TX_PAYLOAD_FIELDS, TX_TRANSFER, txid

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX64_OR_ZERO = re.compile(r"^0{64}$|^[0-9a-f]{64}$")


def _require_str(obj: dict, key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise IngestionError(RejectReason.MALFORMED, f"field {key!r} must be a string")
    return value


def verify_transaction(tx: object, *, height: int) -> None:
    if not isinstance(tx, dict):
        raise IngestionError(RejectReason.MALFORMED, "transaction must be an object")
    for field in TX_PAYLOAD_FIELDS + ("txid", "pubkey", "signature"):
        if field not in tx:
            raise IngestionError(RejectReason.MALFORMED, f"transaction missing field {field!r}")

    tx_type = tx["type"]
    if tx_type not in (TX_TRANSFER, TX_MINT):
        raise IngestionError(RejectReason.BAD_TX_TYPE, f"unknown tx type {tx_type!r}")
    if not isinstance(tx["nonce"], int) or isinstance(tx["nonce"], bool):
        raise IngestionError(RejectReason.MALFORMED, "tx nonce must be an integer")

    # 1) recomputed txid must equal the claimed id
    claimed_txid = _require_str(tx, "txid")
    if not _HEX64.match(claimed_txid):
        raise IngestionError(RejectReason.MALFORMED, "txid must be 64 lower-case hex chars")
    if txid(tx) != claimed_txid:
        raise IngestionError(
            RejectReason.HEADER_MISMATCH,
            "txid does not match canonical transaction body",
        )

    # 2) pubkey derives to the declared sender (and signs the body)
    try:
        pubkey_hex = _require_str(tx, "pubkey")
        pubkey = public_key_from_hex(pubkey_hex)
        signature = bytes.fromhex(_require_str(tx, "signature"))
    except (ValueError, TypeError) as exc:
        raise IngestionError(RejectReason.MALFORMED, f"bad pubkey/signature encoding: {exc}")
    if address_from_pubkey(pubkey_hex) != tx["sender"]:
        raise IngestionError(
            RejectReason.SELF_SENDER_MISMATCH,
            "transaction pubkey does not derive to the declared sender",
        )
    signing_payload = canonical_json({f: tx[f] for f in TX_PAYLOAD_FIELDS})
    if not verify(pubkey, signature, signing_payload):
        raise IngestionError(
            RejectReason.BAD_SIGNATURE,
            f"transaction {claimed_txid[:12]}… signature failed Ed25519 verification",
        )

    if tx_type == TX_MINT and height != 0:
        raise IngestionError(
            RejectReason.MINT_OUTSIDE_GENESIS,
            "mint transactions are valid only in the genesis block",
        )
    if tx_type == TX_TRANSFER and height == 0:
        raise IngestionError(
            RejectReason.MINT_AT_GENESIS_REQUIRED,
            "the genesis block may contain mint transactions only",
        )


def verify_block(block: object, *, allowed_difficulties: set[int], authorized_producers: set[str]) -> str:
    """Statelessly validate a sealed block; return its identity hash hex.

    ``allowed_difficulties`` is the consensus-permitted set of per-block
    weights.  The synthetic fixtures use 4 (regular block) and 16 (weighted
    block); proof-of-work target for a block is ``2^256 // difficulty``.
    """
    if not isinstance(block, dict):
        raise IngestionError(RejectReason.MALFORMED, "block must be an object")

    required = (
        "version", "height", "parent", "merkle_root", "difficulty",
        "timestamp", "producer", "nonce", "pow_signature", "transactions",
    )
    for field in required:
        if field not in block:
            raise IngestionError(RejectReason.MALFORMED, f"block missing field {field!r}")

    if not isinstance(block["height"], int) or isinstance(block["height"], bool) or block["height"] < 0:
        raise IngestionError(RejectReason.MALFORMED, "height must be a non-negative integer")
    if not isinstance(block["version"], int) or block["version"] != 1:
        raise IngestionError(RejectReason.MALFORMED, "only block version 1 is supported")
    if not isinstance(block["timestamp"], str) or not block["timestamp"]:
        raise IngestionError(RejectReason.MALFORMED, "timestamp must be a non-empty string")
    if not _HEX64.match(_require_str(block, "parent")):
        raise IngestionError(RejectReason.MALFORMED, "parent must be 64 hex chars")
    if block["height"] == 0 and block["parent"] != "0" * 64:
        raise IngestionError(RejectReason.MALFORMED, "genesis parent must be the zero hash")
    if not isinstance(block["nonce"], str) or not block["nonce"].isdigit():
        raise IngestionError(RejectReason.MALFORMED, "nonce must be an ASCII decimal string")
    if not isinstance(block["difficulty"], int) or block["difficulty"] <= 0:
        raise IngestionError(RejectReason.MALFORMED, "difficulty must be a positive integer")
    if block["difficulty"] not in allowed_difficulties:
        raise IngestionError(
            RejectReason.BAD_DIFFICULTY,
            f"difficulty {block['difficulty']} not in allowed set {sorted(allowed_difficulties)}",
        )

    # Transactions: order-independent signature checks, then Merkle root.
    transactions = block["transactions"]
    if not isinstance(transactions, list) or not transactions:
        raise IngestionError(RejectReason.MALFORMED, "block must contain a non-empty transactions list")
    seen: set[str] = set()
    for tx in transactions:
        verify_transaction(tx, height=block["height"])
        if tx["txid"] in seen:
            raise IngestionError(
                RejectReason.DUPLICATE_TXID,
                f"txid {tx['txid'][:12]}… appears twice in the same block",
            )
        seen.add(tx["txid"])

    expected_root = merkle_root([tx["txid"] for tx in transactions])
    if _require_str(block, "merkle_root") != expected_root:
        raise IngestionError(
            RejectReason.BAD_MERKLE,
            "header merkle_root does not match the transaction list",
        )

    producer_hex = _require_str(block, "producer")
    try:
        producer_key = public_key_from_hex(producer_hex)
    except (ValueError, TypeError) as exc:
        raise IngestionError(RejectReason.MALFORMED, f"bad producer pubkey: {exc}")
    producer_address = address_from_pubkey(producer_hex)
    if producer_address not in authorized_producers:
        raise IngestionError(
            RejectReason.UNKNOWN_PRODUCER,
            f"producer {producer_address[:10]}… is not in the authorized producer set",
        )

    identity = block_identity_hash(block)
    if not pow_satisfied(identity, block["difficulty"]):
        raise IngestionError(
            RejectReason.BAD_POW,
            f"block hash {identity[:12]}… does not meet difficulty {block['difficulty']}",
        )
    try:
        pow_sig = bytes.fromhex(_require_str(block, "pow_signature"))
    except ValueError as exc:
        raise IngestionError(RejectReason.MALFORMED, f"bad pow_signature hex: {exc}")
    if not verify(producer_key, pow_sig, bytes.fromhex(identity)):
        raise IngestionError(
            RejectReason.BAD_SIGNATURE,
            "producer Ed25519 signature over the block hash failed",
        )
    return identity
