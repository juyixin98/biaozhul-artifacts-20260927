"""Chain-state kernel: account ledger and deterministic block application.

Responsibilities
----------------
* Maintain per-account balance/nonce and the chain's base-fee timeline.
* Validate each synthetic block against its parent (number, gas <= limit,
  header base fee == pure recurrence result).
* Validate every transaction (signature recovers to a known sender, intrinsic
  gas, fee caps, overflow, nonce ordering, sufficient funds).
* Execute admitted transactions with explicit, conserved money movement:

      charge  = effective_gas_price * gas_used + value
      burned  = base_fee * gas_used            (locked/burned, tracked)
      tip     = priority_fee * gas_used        (would go to the producer)
      sent    = value                          (transferred to recipient)

  Conservation per transaction:

      sender_debit == burned + tip + sent

  ``gas_used`` comes from the synthetic block fixture (execution result),
  always satisfying ``intrinsic_gas <= gas_used <= gas_limit``.

This is a *model*: no EVM execution and no live chain. The fee conservation
is exact and asserted in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import DEFAULT_GAS_LIMIT, TX_TYPE_LEGACY
from ..errors import BlockError, FailureCode, TransactionError
from ..encoding import rlp
from ..encoding.crypto import keccak256
from ..encoding.merkle import merkle_root
from ..encoding.transaction import Transaction, decode_transaction
from .fees import gas_target, next_base_fee
from .validation import (fee_quote, intrinsic_gas, tx_max_upfront,
                         validate_fee_caps, validate_intrinsic_gas)


@dataclass
class Account:
    address: bytes
    balance: int = 0
    nonce: int = 0

    def to_dict(self) -> dict:
        return {"address": "0x" + self.address.hex(),
                "balance": self.balance, "nonce": self.nonce}


@dataclass
class TxReceipt:
    tx_hash: bytes
    sender: bytes
    to: bytes
    gas_used: int
    effective_gas_price: int
    base_fee_charged: int
    tip_charged: int
    burned: int
    value: int
    sender_debit: int

    def to_dict(self) -> dict:
        return {
            "tx_hash": "0x" + self.tx_hash.hex(),
            "sender": "0x" + self.sender.hex(),
            "to": "0x" + self.to.hex(),
            "gas_used": self.gas_used,
            "effective_gas_price": self.effective_gas_price,
            "base_fee_charged": self.base_fee_charged,
            "tip_charged": self.tip_charged,
            "burned": self.burned,
            "value": self.value,
            "sender_debit": self.sender_debit,
        }


@dataclass
class Block:
    number: int
    parent_hash: bytes
    base_fee: int
    gas_limit: int
    gas_used: int
    transactions: list[Transaction] = field(default_factory=list)
    # Per-tx executed gas, aligned by index (synthetic execution results).
    tx_gas_used: list[int] = field(default_factory=list)
    tx_count: int = 0
    transactions_root: bytes = b""
    hash: bytes = b""

    # Header fields included in the block hash (number, parent, base fee,
    # gas limit, gas used, tx root) so tampering changes the identity.
    def header_items(self) -> list:
        return [
            rlp.int_to_bytes(self.number),
            self.parent_hash,
            rlp.int_to_bytes(self.base_fee),
            rlp.int_to_bytes(self.gas_limit),
            rlp.int_to_bytes(self.gas_used),
            self.transactions_root,
        ]

    def compute_hash(self) -> bytes:
        return keccak256(rlp.encode(self.header_items()))


@dataclass
class BlockResult:
    number: int
    hash: bytes
    base_fee: int
    next_base_fee: int
    gas_used: int
    gas_limit: int
    gas_target: int
    burned: int
    tips: int
    transferred: int
    total_debited: int
    receipts: list[TxReceipt]

    def summary(self) -> dict:
        return {
            "number": self.number,
            "hash": "0x" + self.hash.hex(),
            "base_fee": self.base_fee,
            "next_base_fee": self.next_base_fee,
            "gas_used": self.gas_used,
            "gas_limit": self.gas_limit,
            "gas_target": self.gas_target,
            "burned": self.burned,
            "tips": self.tips,
            "transferred": self.transferred,
            "total_debited": self.total_debited,
            "tx_count": len(self.receipts),
        }


class ChainState:
    """In-memory authoritative state. Persistence is a separate concern
    (:mod:`basefee_model.storage`)."""

    def __init__(self, genesis_base_fee: int, gas_limit: int = DEFAULT_GAS_LIMIT,
                 genesis_gas_used: int | None = None, chain_id: int | None = None):
        self.accounts: dict[bytes, Account] = {}
        self.blocks: list[Block] = []
        self.results: dict[int, BlockResult] = {}
        self.gas_limit = gas_limit
        self.genesis_base_fee = genesis_base_fee
        # When set, every transaction's signed chain_id must match (EIP-155
        # replay protection). None = accept any chain id.
        self.chain_id = chain_id
        # Running global counters for conservation reporting.
        self.total_burned = 0
        self.total_tips = 0
        self.total_transferred = 0
        # Genesis pseudo-header anchors parent hash and base fee. Its gas used
        # defaults to the target so the first real block's recurrence keeps the
        # genesis base fee (the activation block is treated as balanced); the
        # value can be overridden to exercise an empty-parent first step.
        if genesis_gas_used is None:
            genesis_gas_used = gas_target(gas_limit)
        genesis = Block(
            number=0, parent_hash=b"\x00" * 32, base_fee=genesis_base_fee,
            gas_limit=gas_limit, gas_used=genesis_gas_used,
            transactions=[], tx_gas_used=[],
        )
        genesis.transactions_root = merkle_root([])
        genesis.hash = genesis.compute_hash()
        self.blocks.append(genesis)

    # -- account bootstrap -------------------------------------------------
    def add_account(self, address: bytes, balance: int = 0,
                    nonce: int = 0) -> Account:
        if address in self.accounts:
            acc = self.accounts[address]
            acc.balance = balance
            acc.nonce = nonce
            return acc
        acc = Account(address=address, balance=balance, nonce=nonce)
        self.accounts[address] = acc
        return acc

    def get_account(self, address: bytes) -> Account:
        if address not in self.accounts:
            # An unknown sender has no funds and nonce 0; it cannot pay.
            return Account(address=address, balance=0, nonce=0)
        return self.accounts[address]

    @property
    def head(self) -> Block:
        return self.blocks[-1]

    def base_fee_at_head(self) -> int:
        return self.head.base_fee

    # -- block assembly ----------------------------------------------------
    def build_block(self, raw_txs: list[bytes],
                    tx_gas_used: list[int] | None = None,
                    gas_limit: int | None = None) -> Block:
        """Decode, recover and assemble the *next* block without applying it.

        Header base fee is computed purely from the parent. Validation of
        ordering/gas happens in :meth:`apply_block`.
        """
        number = self.head.number + 1
        gas_limit = self.gas_limit if gas_limit is None else gas_limit
        parent = self.head
        base_fee = next_base_fee(parent.base_fee, parent.gas_used,
                                 parent.gas_limit)

        txs: list[Transaction] = []
        for raw in raw_txs:
            tx = decode_transaction(raw)
            txs.append(tx)
        if tx_gas_used is None:
            # Default: every tx consumes exactly its intrinsic gas.
            tx_gas_used = [intrinsic_gas(t) for t in txs]
        if len(tx_gas_used) != len(txs):
            raise BlockError(
                "tx_gas_used length must match transaction count",
                code=FailureCode.INVALID_FIELDS,
            )
        total_gas = sum(tx_gas_used)
        tx_leaves = [t.tx_hash() for t in txs]
        block = Block(
            number=number, parent_hash=parent.hash, base_fee=base_fee,
            gas_limit=gas_limit, gas_used=total_gas, transactions=txs,
            tx_gas_used=list(tx_gas_used), tx_count=len(txs),
            transactions_root=merkle_root(tx_leaves),
        )
        block.hash = block.compute_hash()
        return block

    # -- block validation / application -----------------------------------
    def apply_block(self, block: Block) -> BlockResult:
        parent = self.head
        if block.number != parent.number + 1:
            raise BlockError(
                "block number is not parent+1",
                code=FailureCode.BAD_BLOCK_NUMBER,
                details={"got": block.number, "expected": parent.number + 1},
            )
        if block.parent_hash != parent.hash:
            raise BlockError(
                "parent hash mismatch",
                code=FailureCode.BAD_PARENT,
                details={"got": block.parent_hash.hex(),
                         "want": parent.hash.hex()},
            )
        expected_base = next_base_fee(parent.base_fee, parent.gas_used,
                                      parent.gas_limit)
        if block.base_fee != expected_base:
            raise BlockError(
                "block base fee does not match parent-derived recurrence",
                code=FailureCode.BAD_BASE_FEE,
                details={"got": block.base_fee, "expected": expected_base},
            )
        if block.gas_used < 0:
            raise BlockError("negative gas used",
                             code=FailureCode.BLOCK_GAS_NEGATIVE)
        if block.gas_used > block.gas_limit:
            raise BlockError(
                "block gas used exceeds gas limit",
                code=FailureCode.BLOCK_GAS_OVER_LIMIT,
                details={"gas_used": block.gas_used,
                         "gas_limit": block.gas_limit},
            )

        # Rebind the tx root in case the block was built externally.
        block.transactions_root = merkle_root([t.tx_hash()
                                               for t in block.transactions])

        receipts: list[TxReceipt] = []
        block_burned = block_tips = block_value = block_debit = 0
        # Per-sender nonce expectations within the block start from state.
        sum_gas = 0
        for idx, tx in enumerate(block.transactions):
            receipt = self._apply_tx(tx, block.base_fee, block.tx_gas_used[idx])
            receipts.append(receipt)
            block_burned += receipt.burned
            block_tips += receipt.tip_charged
            block_value += receipt.value
            block_debit += receipt.sender_debit
            sum_gas += receipt.gas_used

        if sum_gas != block.gas_used:
            raise BlockError(
                "declared gas_used does not match sum of per-tx gas",
                code=FailureCode.INVALID_FIELDS,
                details={"declared": block.gas_used, "sum": sum_gas},
            )

        block.hash = block.compute_hash()
        result = BlockResult(
            number=block.number, hash=block.hash, base_fee=block.base_fee,
            next_base_fee=next_base_fee(block.base_fee, block.gas_used,
                                        block.gas_limit),
            gas_used=block.gas_used, gas_limit=block.gas_limit,
            gas_target=gas_target(block.gas_limit), burned=block_burned,
            tips=block_tips, transferred=block_value,
            total_debited=block_debit, receipts=receipts,
        )

        # Commit atomically: only reach here if every tx applied.
        self.blocks.append(block)
        self.results[block.number] = result
        self.total_burned += block_burned
        self.total_tips += block_tips
        self.total_transferred += block_value
        return result

    def _apply_tx(self, tx: Transaction, base_fee: int,
                  gas_used: int) -> TxReceipt:
        # 1) signature -> sender (must be a known funded account in fixture).
        sender_addr = tx.sender()
        sender = self.get_account(sender_addr)

        # 1b) EIP-155 replay protection: signed chain id must match this chain.
        if self.chain_id is not None and tx.chain_id != self.chain_id:
            raise TransactionError(
                "transaction chain_id does not match the chain",
                code=FailureCode.CHAIN_ID_MISMATCH,
                details={"tx_chain_id": tx.chain_id,
                         "chain_id": self.chain_id},
            )

        # 2) intrinsic gas and per-tx gas bounds.
        validate_intrinsic_gas(tx)
        if gas_used < intrinsic_gas(tx):
            raise TransactionError(
                "executed gas below intrinsic gas",
                code=FailureCode.GAS_LIMIT_EXCEEDED_INTRINSIC,
                details={"gas_used": gas_used,
                         "intrinsic_gas": intrinsic_gas(tx)},
            )
        if gas_used > tx.gas_limit:
            raise TransactionError(
                "executed gas exceeds tx gas_limit",
                code=FailureCode.BLOCK_GAS_OVER_LIMIT,
                details={"gas_used": gas_used, "gas_limit": tx.gas_limit},
            )

        # 3) fee caps and overflow.
        validate_fee_caps(tx, base_fee)

        # 4) nonce ordering (strict, sequential in this model).
        if tx.nonce < sender.nonce:
            raise TransactionError(
                "nonce too low", code=FailureCode.NONCE_TOO_LOW,
                details={"got": tx.nonce, "expected": sender.nonce},
            )
        if tx.nonce > sender.nonce:
            raise TransactionError(
                "nonce too high (gap)", code=FailureCode.NONCE_TOO_HIGH,
                details={"got": tx.nonce, "expected": sender.nonce},
            )

        # 5) sufficient funds for the full up-front reservation.
        upfront = tx_max_upfront(tx)
        if sender.balance < upfront:
            raise TransactionError(
                "insufficient funds for up-front reservation",
                code=FailureCode.INSUFFICIENT_FUNDS,
                details={"balance": sender.balance, "required": upfront,
                         "fee_cap": (tx.gas_price if tx.type == TX_TYPE_LEGACY
                                    else tx.max_fee_per_gas)},
            )

        # 6) execute: actual charge at the effective price for gas used.
        quote = fee_quote(tx, base_fee)
        gas_fee = quote.effective_gas_price * gas_used
        value = tx.value
        tip = quote.priority_fee_per_gas * gas_used
        if len(tx.to) == 20:
            burned = quote.base_fee_per_gas * gas_used
            delivered_value = value
            recipient = self.accounts.setdefault(
                tx.to, Account(address=tx.to, balance=0, nonce=0))
            recipient.balance += value
        else:
            # Contract-creation in the model: no EVM, so the value is also
            # treated as burned (documented boundary), not delivered.
            burned = quote.base_fee_per_gas * gas_used + value
            delivered_value = 0
        debit = gas_fee + value
        # Conservation guard (defensive; should be mathematically impossible).
        if debit != burned + tip + delivered_value:
            raise BlockError("internal fee conservation mismatch",
                             code=FailureCode.INVALID_FIELDS)
        if debit > sender.balance:
            raise TransactionError(
                "insufficient funds at execution",
                code=FailureCode.INSUFFICIENT_FUNDS,
                details={"balance": sender.balance, "debit": debit},
            )

        sender.balance -= debit
        sender.nonce += 1

        return TxReceipt(
            tx_hash=tx.tx_hash(), sender=sender_addr, to=tx.to,
            gas_used=gas_used,
            effective_gas_price=quote.effective_gas_price,
            base_fee_charged=quote.base_fee_per_gas,
            tip_charged=tip, burned=burned, value=delivered_value,
            sender_debit=debit,
        )

    # -- chain-wide conservation audit ------------------------------------
    def conservation_report(self) -> dict:
        """Self-auditing invariant: total debits == burned + tips + value."""
        total_debit = sum(r.total_debited for r in self.results.values())
        total_burned = sum(r.burned for r in self.results.values())
        total_tips = sum(r.tips for r in self.results.values())
        total_value = sum(r.transferred for r in self.results.values())
        conserved = total_debit == total_burned + total_tips + total_value
        return {
            "total_debited": total_debit,
            "total_burned": total_burned,
            "total_tips": total_tips,
            "total_transferred": total_value,
            "conserved": conserved,
            "difference": total_debit - (total_burned + total_tips + total_value),
        }
