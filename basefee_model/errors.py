"""Typed failure categories.

Tests assert against these *specific* categories rather than a boolean
"failed", so the distinction between, e.g., a malformed payload, a bad
signature, an over-full block and an invalid fee cap is observable and
stable. Every failure the kernel can raise is enumerated here.
"""

from __future__ import annotations

from enum import Enum


class FailureCode(str, Enum):
    # --- encoding / signature layer ----------------------------------------
    MALFORMED_RLP = "malformed_rlp"
    UNSUPPORTED_TX_TYPE = "unsupported_tx_type"
    BAD_SIGNATURE = "bad_signature"              # not a recoverable secp256k1 sig
    SIGNER_MISMATCH = "signer_mismatch"          # recovered sender != claimed sender
    CHAIN_ID_MISMATCH = "chain_id_mismatch"      # signed chain id != chain (replay)
    INVALID_FIELDS = "invalid_fields"            # negative / oversize scalar

    # --- transaction-level validity ---------------------------------------
    GAS_LIMIT_EXCEEDED_INTRINSIC = "gas_limit_exceeds_intrinsic"
    FEE_CAP_LESS_THAN_PRIORITY = "fee_cap_less_than_priority"
    FEE_CAP_BELOW_BASE_FEE = "fee_cap_below_base_fee"
    FEE_CAP_OVERFLOWS_U256 = "fee_cap_overflows_u256"
    PRIORITY_CAP_OVERFLOWS_U256 = "priority_cap_overflows_u256"
    NONCE_TOO_LOW = "nonce_too_low"
    NONCE_TOO_HIGH = "nonce_too_high"
    INSUFFICIENT_FUNDS = "insufficient_funds"

    # --- block-level validity ---------------------------------------------
    BLOCK_GAS_OVER_LIMIT = "block_gas_over_limit"
    BLOCK_GAS_NEGATIVE = "block_gas_negative"
    BAD_BASE_FEE = "bad_base_fee"                # header base fee != recurrence
    BAD_BLOCK_NUMBER = "bad_block_number"
    BAD_PARENT = "bad_parent"
    DUPLICATE_BLOCK = "duplicate_block"
    EMPTY_CHAIN = "empty_chain"


class ModelError(Exception):
    """Base error carrying a stable, machine-readable ``code``."""

    code: FailureCode = FailureCode.INVALID_FIELDS

    def __init__(self, message: str, *, code: FailureCode | None = None,
                 details: dict | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code.value, "message": self.message,
                "details": self.details}


class EncodingError(ModelError):
    code = FailureCode.MALFORMED_RLP


class SignatureError(ModelError):
    code = FailureCode.BAD_SIGNATURE


class TransactionError(ModelError):
    code = FailureCode.INVALID_FIELDS


class BlockError(ModelError):
    code = FailureCode.BAD_PARENT
