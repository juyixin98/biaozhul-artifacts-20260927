"""Stable error categories.

Tests assert on these string codes, not on free-text messages, so the wording
of human-readable diagnostics may evolve without breaking reviewability.
"""
from __future__ import annotations

from enum import Enum


class RejectReason(str, Enum):
    # Structural / decoding
    MALFORMED = "MALFORMED"
    BAD_SIGNATURE = "BAD_SIGNATURE"
    UNKNOWN_PRODUCER = "UNKNOWN_PRODUCER"
    BAD_POW = "BAD_POW"
    BAD_DIFFICULTY = "BAD_DIFFICULTY"
    BAD_MERKLE = "BAD_MERKLE"
    HEADER_MISMATCH = "HEADER_MISMATCH"

    # Chain topology
    PARENT_UNKNOWN = "PARENT_UNKNOWN"
    ALREADY_KNOWN = "ALREADY_KNOWN"
    HEIGHT_MISMATCH = "HEIGHT_MISMATCH"
    SECOND_GENESIS = "SECOND_GENESIS"

    # Transaction semantics
    DUPLICATE_TXID = "DUPLICATE_TXID"
    BAD_TX_TYPE = "BAD_TX_TYPE"
    MINT_OUTSIDE_GENESIS = "MINT_OUTSIDE_GENESIS"
    MINT_AT_GENESIS_REQUIRED = "MINT_AT_GENESIS_REQUIRED"
    NONCE_REUSED = "NONCE_REUSED"
    BAD_NONCE_ORDER = "BAD_NONCE_ORDER"
    AMOUNT_INVALID = "AMOUNT_INVALID"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    SELF_SENDER_MISMATCH = "SELF_SENDER_MISMATCH"

    # Fork-choice / finality
    REORG_FINALIZED = "REORG_FINALIZED"


class Outcome(str, Enum):
    ACCEPT_EXTEND = "ACCEPT_EXTEND"        # appended directly to active tip
    ACCEPT_SWITCH = "ACCEPT_SWITCH"        # applied after a fork switch
    ACCEPT_FORK = "ACCEPT_FORK"            # valid, stored on a non-active fork
    PENDING = "PENDING"                    # parent unknown -> suspended
    DUPLICATE = "DUPLICATE"                # block hash already present
    REJECTED = "REJECTED"                  # validation failed, see reason


class IngestionError(Exception):
    def __init__(self, reason: RejectReason, detail: str, **state: object):
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail
        self.state = state
