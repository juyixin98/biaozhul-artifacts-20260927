"""Typed errors for the chain kernel."""
from __future__ import annotations


class ChainError(Exception):
    error_code = "chain_error"


class BadSignature(ChainError):
    error_code = "bad_signature"


class InvalidSender(ChainError):
    error_code = "invalid_sender"


class NonceMismatch(ChainError):
    error_code = "nonce_mismatch"


class InsufficientBalance(ChainError):
    error_code = "insufficient_balance"


class InsufficientAllowance(ChainError):
    error_code = "insufficient_allowance"


class InvalidCalldata(ChainError):
    error_code = "invalid_calldata"
