"""Error contract shared across every module boundary.

All failures raised to another module are :class:`LedgerError` carrying a stable
machine-readable :class:`ErrorCode`. Codes are grouped into four
:class:`ErrorCategory` buckets required by the task:

* ``INPUT``      -- malformed caller input / encoding / signature problems.
* ``STATE``      -- conflicts with committed chain state (double spend, unknown
                    outpoint, orphan, wrong height, balance/fee violation ...).
* ``RESOURCE``   -- configured limits exhausted (size/count/loop/connection).
* ``COMPUTATION``-- crypto/library/serialization failures inside the machinery.

The category is derivable from the code and is also written to run logs so a
replay can be filtered by failure class.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping


class ErrorCategory(str, Enum):
    INPUT = "input"
    STATE = "state"
    RESOURCE = "resource"
    COMPUTATION = "computation"


class ErrorCode(str, Enum):
    # --- INPUT: malformed bytes / structures --------------------------------
    MALFORMED = "malformed"                # generic decode failure
    EMPTY_BLOCK = "empty_block"
    BAD_VERSION = "bad_version"
    BAD_HEADER = "bad_header"
    BAD_TXID = "bad_txid"
    BAD_PUBLIC_KEY = "bad_public_key"
    BAD_SIGNATURE = "bad_signature"
    INVALID_ENCODING = "invalid_encoding"  # trailing bytes / bad length prefix
    VALUE_OUT_OF_RANGE = "value_out_of_range"  # negative or > MAX_MONEY
    ZERO_VALUE_OUTPUT = "zero_value_output"
    NOT_FOUND = "not_found"                  # read-path lookup miss

    # --- STATE: conflicts with ledger state / semantic rules ----------------
    UNKNOWN_OUTPOINT = "unknown_outpoint"
    ALREADY_SPENT = "already_spent"        # spends a committed-then-spent utxo
    DUPLICATE_INPUT = "duplicate_input"    # same outpoint twice in one block
    DUPLICATE_TXID = "duplicate_txid"      # identical txid twice in one block
    DOUBLE_SPEND = "double_spend"          # umbrella code kept for API users
    FORWARD_REFERENCE = "forward_reference"
    CYCLIC_REFERENCE = "cyclic_reference"
    ORPHAN_TRANSACTION = "orphan_transaction"
    BAD_COINBASE = "bad_coinbase"
    BALANCE_MISMATCH = "balance_mismatch"  # conservation violated / bad fee
    FEE_NEGATIVE = "fee_negative"
    VALUE_OVERFLOW = "value_overflow"      # summed amounts overflow u64
    BLOCK_HASH_INVALID = "block_hash_invalid"
    SIG_TAMPERED = "sig_tampered"          # signature proves invalid for msg
    BLOCK_HEIGHT_INVALID = "block_height_invalid"
    PREV_BLOCK_HASH_MISMATCH = "prev_block_hash_mismatch"
    DUPLICATE_BLOCK = "duplicate_block"

    # --- RESOURCE: limits ----------------------------------------------------
    BLOCK_TOO_LARGE = "block_too_large"
    TOO_MANY_TXS = "too_many_txs"
    TOO_MANY_INPUTS = "too_many_inputs"
    TOO_MANY_OUTPUTS = "too_many_outputs"
    TOO_MANY_SIGOPS = "too_many_sigops"
    BLOCK_VALIDATION_STEPS_EXCEEDED = "block_validation_steps_exceeded"

    # --- COMPUTATION: machinery / library -----------------------------------
    CRYPTO_FAILURE = "crypto_failure"
    STORAGE_FAILURE = "storage_failure"
    SERIALIZATION_FAILURE = "serialization_failure"
    INTERNAL_ERROR = "internal_error"


# Explicit code -> category map (no silent "default" bucket).
_CATEGORY: dict[ErrorCode, ErrorCategory] = {
    # INPUT
    ErrorCode.MALFORMED: ErrorCategory.INPUT,
    ErrorCode.EMPTY_BLOCK: ErrorCategory.INPUT,
    ErrorCode.BAD_VERSION: ErrorCategory.INPUT,
    ErrorCode.BAD_HEADER: ErrorCategory.INPUT,
    ErrorCode.BAD_TXID: ErrorCategory.INPUT,
    ErrorCode.BAD_PUBLIC_KEY: ErrorCategory.INPUT,
    ErrorCode.BAD_SIGNATURE: ErrorCategory.INPUT,
    ErrorCode.INVALID_ENCODING: ErrorCategory.INPUT,
    ErrorCode.VALUE_OUT_OF_RANGE: ErrorCategory.INPUT,
    ErrorCode.ZERO_VALUE_OUTPUT: ErrorCategory.INPUT,
    ErrorCode.NOT_FOUND: ErrorCategory.INPUT,
    # STATE
    ErrorCode.UNKNOWN_OUTPOINT: ErrorCategory.STATE,
    ErrorCode.ALREADY_SPENT: ErrorCategory.STATE,
    ErrorCode.DUPLICATE_INPUT: ErrorCategory.STATE,
    ErrorCode.DUPLICATE_TXID: ErrorCategory.STATE,
    ErrorCode.DOUBLE_SPEND: ErrorCategory.STATE,
    ErrorCode.FORWARD_REFERENCE: ErrorCategory.STATE,
    ErrorCode.CYCLIC_REFERENCE: ErrorCategory.STATE,
    ErrorCode.ORPHAN_TRANSACTION: ErrorCategory.STATE,
    ErrorCode.BAD_COINBASE: ErrorCategory.STATE,
    ErrorCode.BALANCE_MISMATCH: ErrorCategory.STATE,
    ErrorCode.FEE_NEGATIVE: ErrorCategory.STATE,
    ErrorCode.VALUE_OVERFLOW: ErrorCategory.STATE,
    ErrorCode.BLOCK_HASH_INVALID: ErrorCategory.STATE,
    ErrorCode.SIG_TAMPERED: ErrorCategory.STATE,
    ErrorCode.BLOCK_HEIGHT_INVALID: ErrorCategory.STATE,
    ErrorCode.PREV_BLOCK_HASH_MISMATCH: ErrorCategory.STATE,
    ErrorCode.DUPLICATE_BLOCK: ErrorCategory.STATE,
    # RESOURCE
    ErrorCode.BLOCK_TOO_LARGE: ErrorCategory.RESOURCE,
    ErrorCode.TOO_MANY_TXS: ErrorCategory.RESOURCE,
    ErrorCode.TOO_MANY_INPUTS: ErrorCategory.RESOURCE,
    ErrorCode.TOO_MANY_OUTPUTS: ErrorCategory.RESOURCE,
    ErrorCode.TOO_MANY_SIGOPS: ErrorCategory.RESOURCE,
    ErrorCode.BLOCK_VALIDATION_STEPS_EXCEEDED: ErrorCategory.RESOURCE,
    # COMPUTATION
    ErrorCode.CRYPTO_FAILURE: ErrorCategory.COMPUTATION,
    ErrorCode.STORAGE_FAILURE: ErrorCategory.COMPUTATION,
    ErrorCode.SERIALIZATION_FAILURE: ErrorCategory.COMPUTATION,
    ErrorCode.INTERNAL_ERROR: ErrorCategory.COMPUTATION,
}


def category_of(code: ErrorCode) -> ErrorCategory:
    try:
        return _CATEGORY[code]
    except KeyError:  # pragma: no cover - every code is mapped above
        return ErrorCategory.COMPUTATION


class LedgerError(Exception):
    """Raised across module boundaries. Never carries a bare string alone."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        tx_index: int | None = None,
        outpoint: tuple[bytes, int] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category_of(code)
        self.message = message
        self.tx_index = tx_index
        self.outpoint = outpoint
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "code": self.code.value,
            "category": self.category.value,
            "message": self.message,
        }
        if self.tx_index is not None:
            d["tx_index"] = self.tx_index
        if self.outpoint is not None:
            txid, vout = self.outpoint
            d["outpoint"] = {"txid": txid.hex(), "vout": vout}
        if self.details:
            d["details"] = self.details
        return d

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        loc = ""
        if self.tx_index is not None:
            loc = f" [tx[{self.tx_index}]]"
        return f"{self.category.value}/{self.code.value}: {self.message}{loc}"
