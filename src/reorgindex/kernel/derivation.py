"""Derivation of revocable ledger events from a block.

The derived index is intentionally a separate projection from the consensus
state machine: given an accepted block it produces an ordered list of event
rows, and each row's inverse is what a reorg rollback applies.
"""
from __future__ import annotations

from .models import TX_MINT, TX_TRANSFER


def block_ledger_deltas(block: dict) -> list[dict]:
    """Ordered event rows for one accepted block.

    Positions are globally unique within a block:

    * 2*i + 0 -> sender debit (and nonce)
    * 2*i + 1 -> recipient credit
    * 2*i + 2 -> fee credit (only when fee > 0)

    Mints emit a single credit at position 2*i.
    """
    deltas: list[dict] = []
    for i, tx in enumerate(block["transactions"]):
        amount = int(tx["amount"])
        fee = int(tx["fee"])
        tx_id = tx["txid"]
        if tx["type"] == TX_MINT:
            deltas.append(
                {
                    "txid": tx_id,
                    "position_in_block": 2 * i,
                    "kind": "credit",
                    "address": tx["recipient"],
                    "amount_delta": amount,
                    "nonce_delta": 0,
                }
            )
            continue

        assert tx["type"] == TX_TRANSFER
        deltas.append(
            {
                "txid": tx_id,
                "position_in_block": 2 * i,
                "kind": "debit",
                "address": tx["sender"],
                "amount_delta": -(amount + fee),
                "nonce_delta": 1,
            }
        )
        deltas.append(
            {
                "txid": tx_id,
                "position_in_block": 2 * i + 1,
                "kind": "credit",
                "address": tx["recipient"],
                "amount_delta": amount,
                "nonce_delta": 0,
            }
        )
        if fee > 0:
            deltas.append(
                {
                    "txid": tx_id,
                    "position_in_block": 2 * i + 2,
                    "kind": "fee",
                    "address": tx["fee_recipient"],
                    "amount_delta": fee,
                    "nonce_delta": 0,
                }
            )
    return deltas


def txids_of(block: dict) -> list[str]:
    return [tx["txid"] for tx in block["transactions"]]
