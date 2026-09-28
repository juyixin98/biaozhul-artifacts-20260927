"""Kernel domain objects: signed transactions and blocks.

These are the objects the state machine consumes. They are intentionally plain
dataclasses over Python ``int`` (unbounded) so overflow is an *explicit* check
(E032), not a silent wrap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Signature:
    r: int
    s: int
    v: int  # recovery parity in {0,1}


@dataclass(frozen=True)
class Transaction:
    chain_id: int
    nonce: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    gas_limit: int
    to: str            # 0x-prefixed 20-byte hex address
    value: int
    data: bytes = b""
    signature: Optional[Signature] = None

    def claimed_sender(self) -> str:
        """Sender is *claimed* by the signature until the kernel recovers it."""
        return getattr(self, "_sender_override", None)


@dataclass
class TxReceipt:
    tx_hash: str
    sender: str
    valid: bool
    error_code: Optional[str]
    # Economics (populated when valid):
    gas: int = 0
    effective_priority_tip: int = 0
    effective_gas_price: int = 0
    burned: int = 0
    tip: int = 0
    total_cost: int = 0


@dataclass
class Block:
    number: int
    parent_hash: str
    base_fee_per_gas: int
    gas_limit: int
    gas_used: int
    transactions: list[Transaction] = field(default_factory=list)
    block_hash: Optional[str] = None


@dataclass
class InvalidTxRecord:
    index: int
    tx_hash: str
    error_code: str
    detail: str


@dataclass
class ExecutedBlock:
    block: Block
    receipts: list[TxReceipt]
    invalid: list[InvalidTxRecord]
    next_base_fee: int
    fee_step_explanation: dict
    accepted: bool
    block_error: Optional[str] = None
    block_error_detail: Optional[str] = None
    # Conservation ledger (system-wide balances):
    total_burned: int = 0
    total_tipped: int = 0
    balances_after: dict[str, int] = field(default_factory=dict)
    burned_address: str = "0x" + "00" * 20
