"""Account/UTXO-light chain-state machine used to *validate* candidate blocks.

The derived index in storage is built independently from the ledger events;
this module exists so an invalid block can never poison the index.  Balances
are integer units; an account nonce is the count of accepted transfers from
that address (genesis mints do not consume a nonce).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import IngestionError, RejectReason
from .models import TX_MINT, TX_TRANSFER


def _amount(name: str, raw: object) -> int:
    if isinstance(raw, bool):  # bool is an int subclass -- refuse explicitly
        raise IngestionError(RejectReason.AMOUNT_INVALID, f"{name} must be a decimal string")
    if not isinstance(raw, str) or not raw.isdigit():
        raise IngestionError(RejectReason.AMOUNT_INVALID, f"{name} must be a non-negative decimal string")
    value = int(raw)
    if value <= 0 and name == "amount":
        raise IngestionError(RejectReason.AMOUNT_INVALID, "amount must be positive")
    if value < 0:
        raise IngestionError(RejectReason.AMOUNT_INVALID, f"{name} must be non-negative")
    return value


@dataclass
class Account:
    balance: int = 0
    nonce: int = 0

    def clone(self) -> "Account":
        return Account(self.balance, self.nonce)


@dataclass
class ChainState:
    """State at the tip of some chain (active or a candidate fork)."""

    accounts: dict[str, Account] = field(default_factory=dict)
    txids: set[str] = field(default_factory=set)

    def account(self, address: str) -> Account:
        acct = self.accounts.get(address)
        if acct is None:
            acct = Account()
            self.accounts[address] = acct
        return acct

    def clone(self) -> "ChainState":
        return ChainState(
            accounts={addr: a.clone() for addr, a in self.accounts.items()},
            txids=set(self.txids),
        )

    def snapshot_balances(self) -> dict[str, int]:
        return {addr: a.balance for addr, a in self.accounts.items()}

    # ------------------------------------------------------------------
    def apply_tx(self, tx: dict, *, height: int) -> None:
        """Validate one transaction against this state and mutate it."""
        tx_type = tx.get("type")
        if tx_type not in (TX_TRANSFER, TX_MINT):
            raise IngestionError(RejectReason.BAD_TX_TYPE, f"unknown tx type {tx_type!r}")

        tx_id = tx.get("txid")
        if not isinstance(tx_id, str) or len(tx_id) != 64:
            raise IngestionError(RejectReason.MALFORMED, "txid missing or malformed")
        if tx_id in self.txids:
            # Same txid in the chain that is being built: no second contribution.
            raise IngestionError(
                RejectReason.DUPLICATE_TXID,
                f"txid {tx_id[:12]}… already present in this chain",
            )

        sender = tx["sender"]
        recipient = tx["recipient"]
        fee_recipient = tx["fee_recipient"]
        amount = _amount("amount", tx.get("amount"))
        fee = _amount("fee", tx.get("fee"))

        if tx_type == TX_MINT:
            if height != 0:
                raise IngestionError(
                    RejectReason.MINT_OUTSIDE_GENESIS,
                    "mint transactions are valid only in the genesis block",
                )
            self.account(recipient).balance += amount
            self.txids.add(tx_id)
            return

        # transfer
        if height == 0:
            raise IngestionError(
                RejectReason.MINT_AT_GENESIS_REQUIRED,
                "the genesis block may contain mint transactions only",
            )
        declared_nonce = tx.get("nonce")
        if not isinstance(declared_nonce, int) or isinstance(declared_nonce, bool):
            raise IngestionError(RejectReason.MALFORMED, "nonce must be an integer")
        sender_acct = self.account(sender)
        if declared_nonce != sender_acct.nonce + 1:
            if declared_nonce <= sender_acct.nonce:
                raise IngestionError(
                    RejectReason.NONCE_REUSED,
                    f"nonce {declared_nonce} for {sender[:8]}… already used",
                    expected=sender_acct.nonce + 1,
                    got=declared_nonce,
                )
            raise IngestionError(
                RejectReason.BAD_NONCE_ORDER,
                f"nonce {declared_nonce} skips ahead of {sender_acct.nonce + 1}",
                expected=sender_acct.nonce + 1,
                got=declared_nonce,
            )
        if amount + fee > sender_acct.balance:
            raise IngestionError(
                RejectReason.INSUFFICIENT_FUNDS,
                f"sender {sender[:8]}… has {sender_acct.balance}, needs {amount + fee}",
                balance=sender_acct.balance,
                required=amount + fee,
            )
        sender_acct.balance -= amount + fee
        sender_acct.nonce += 1
        self.account(recipient).balance += amount
        if fee:
            self.account(fee_recipient).balance += fee
        self.txids.add(tx_id)

    def apply_block(self, block: dict) -> None:
        height = block["height"]
        for tx in block["transactions"]:
            self.apply_tx(tx, height=height)


def genesis_state(genesis_block: dict) -> ChainState:
    state = ChainState()
    state.apply_block(genesis_block)
    return state
