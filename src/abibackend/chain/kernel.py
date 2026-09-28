"""Chain state kernel.

A minimal but real state machine over a synthetic fungible-token contract.
Transactions carry calldata encoded with *this* package's ABI encoder; the
kernel decodes them with the strict decoder, verifies a secp256k1 signature
over a canonical ABI-encoded preimage, checks nonce/balance, mutates state and
returns a deterministic result + events. No EVM, no network, no real accounts.

Supported calls
---------------
* ``transfer(address to, uint256 amount)``
* ``approve(address spender, uint256 amount)``
* ``transferFrom(address from, address to, uint256 amount)``

The kernel never turns an exception into a success: ``apply_transaction`` either
returns a :class:`Receipt` with ``ok=True`` and an event, or raises a typed
:class:`ChainError`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..abi import (
    OffsetOutOfBounds,
    OffsetOverlap,
    LengthTooLarge,
    NonCanonicalEncoding,
    NonCanonicalPadding,
    TrailingBytes,
    ValueOutOfRange,
    encode,
    decode,
)
from ..crypto import Signature, keccak256, recover_address, sign_digest
from .errors import (
    BadSignature,
    ChainError,
    InsufficientAllowance,
    InsufficientBalance,
    InvalidCalldata,
    InvalidSender,
    NonceMismatch,
)

TOKEN_TOTAL_SUPPLY = 1_000_000 * 10**18

# Selector -> (name, arg types). Selectors are computed through the codec at
# import time using the mature Keccak backend.
_FUNCTIONS = {
    "transfer": ["address", "uint256"],
    "approve": ["address", "uint256"],
    "transferFrom": ["address", "address", "uint256"],
}


def _selector(name: str) -> bytes:
    from ..abi import function_selector

    return function_selector(name, _FUNCTIONS[name])


SELECTORS = {_selector(name): name for name in _FUNCTIONS}

# Canonical fields hashed to sign a transaction (no RLP; explicit ABI tuple).
SIGNATURE_TYPES = ["uint256", "uint256", "address", "bytes", "uint256"]
# chain_id, nonce, sender(address int), calldata(bytes), gas_price


@dataclass
class Transaction:
    chain_id: int
    nonce: int
    sender: int  # address as uint160 integer
    calldata: bytes
    gas_price: int
    signature: Signature

    def signing_hash(self) -> bytes:
        packed = encode(
            SIGNATURE_TYPES,
            [self.chain_id, self.nonce, self.sender, self.calldata, self.gas_price],
        )
        return keccak256(packed)


@dataclass
class Event:
    name: str
    args: Dict[str, object]


@dataclass
class Receipt:
    ok: bool
    sender: int
    nonce: int
    call: str
    decoded: Tuple[object, ...]
    events: List[Event] = field(default_factory=list)
    state_root: bytes = b""


@dataclass
class Account:
    balance: int = 0
    nonce: int = 0
    # owner -> spender -> allowance
    allowances: Dict[int, int] = field(default_factory=dict)


@dataclass
class ChainState:
    chain_id: int
    accounts: Dict[int, Account] = field(default_factory=dict)

    def account(self, addr: int) -> Account:
        acct = self.accounts.get(addr)
        if acct is None:
            acct = Account()
            self.accounts[addr] = acct
        return acct

    def state_root(self) -> bytes:
        """Deterministic keccak commitment over the sorted account set.

        Each account leaf encodes address, balance, nonce and a flat
        ``[spender,amount, spender,amount, ...]`` allowance array. Leaves are
        ordered by address, concatenated, and hashed once more.
        """
        leaves: List[bytes] = []
        for addr in sorted(self.accounts):
            acct = self.accounts[addr]
            flat: List[int] = []
            for spender in sorted(acct.allowances):
                flat.append(spender)
                flat.append(acct.allowances[spender])
            block = encode(
                ["address", "uint256", "uint256", "uint256[]"],
                [addr, acct.balance, acct.nonce, flat],
            )
            leaves.append(keccak256(block))
        if not leaves:
            return keccak256(b"empty-state-v1")
        return keccak256(b"".join(leaves))


def make_bootstrap_state(chain_id: int, allocations: Dict[int, int]) -> ChainState:
    state = ChainState(chain_id=chain_id)
    total = 0
    for addr, amount in allocations.items():
        state.account(addr).balance = amount
        total += amount
    if total > TOKEN_TOTAL_SUPPLY:
        raise ValueError("allocations exceed token total supply")
    return state


def build_transaction(
    chain_id: int,
    nonce: int,
    sender: int,
    call: str,
    args: List[object],
    gas_price: int,
    privkey: bytes,
) -> Transaction:
    """Encode a call and sign it with ``privkey`` (helper for fixtures/clients)."""
    from ..abi import encode_call

    calldata = encode_call(call, _FUNCTIONS[call], args)
    tx = Transaction(
        chain_id=chain_id,
        nonce=nonce,
        sender=sender,
        calldata=calldata,
        gas_price=gas_price,
        signature=Signature(0, 0, 0),
    )
    digest = tx.signing_hash()
    tx.signature = sign_digest(privkey, digest)
    return tx


# --------------------------------------------------------------------------- #
# Decode / verify
# --------------------------------------------------------------------------- #
_DECODE_ERRORS = (
    OffsetOutOfBounds,
    OffsetOverlap,
    LengthTooLarge,
    NonCanonicalEncoding,
    NonCanonicalPadding,
    TrailingBytes,
    ValueOutOfRange,
)


def _decode_call(calldata: bytes) -> Tuple[str, Tuple[object, ...]]:
    if len(calldata) < 4:
        raise InvalidCalldata("calldata shorter than 4-byte selector")
    selector, argblob = calldata[:4], calldata[4:]
    name = SELECTORS.get(selector)
    if name is None:
        raise InvalidCalldata(f"unknown selector {selector.hex()}")
    try:
        args = decode(_FUNCTIONS[name], argblob)
    except _DECODE_ERRORS as exc:
        # Preserve the precise failure category rather than a generic reject.
        raise InvalidCalldata(f"{type(exc).error_code}: {exc}") from exc
    return name, args


def verify_signature(tx: Transaction) -> int:
    """Return the recovered sender; raise BadSignature if it mismatches."""
    try:
        recovered = recover_address(tx.signing_hash(), tx.signature)
    except Exception as exc:  # malformed curve points, etc.
        raise BadSignature(f"signature not recoverable: {exc}") from exc
    recovered_int = int.from_bytes(recovered, "big")
    if recovered_int != tx.sender:
        raise BadSignature("signer does not match declared sender")
    return recovered_int


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
def apply_transaction(state: ChainState, tx: Transaction) -> Receipt:
    if tx.chain_id != state.chain_id:
        raise ChainError(
            f"chain_id {tx.chain_id} != state {state.chain_id}"
        )
    if tx.gas_price < 0:
        raise ChainError("gas_price must be non-negative")

    sender = verify_signature(tx)
    acct = state.account(sender)
    if acct.nonce != tx.nonce:
        raise NonceMismatch(f"expected nonce {acct.nonce}, got {tx.nonce}")

    name, args = _decode_call(tx.calldata)

    events: List[Event] = []
    if name == "transfer":
        to, amount = args
        _transfer(state, sender, int(to), int(amount), events)
    elif name == "approve":
        spender, amount = args
        acct.allowances[int(spender)] = int(amount)
        events.append(Event("Approval", {"owner": sender, "spender": int(spender), "value": int(amount)}))
    elif name == "transferFrom":
        frm, to, amount = args
        _transfer_from(state, sender, int(frm), int(to), int(amount), events)
    else:  # pragma: no cover - selector table already gates this
        raise InvalidCalldata(f"unrouted call {name}")

    acct.nonce += 1
    return Receipt(
        ok=True,
        sender=sender,
        nonce=tx.nonce,
        call=name,
        decoded=args,
        events=events,
        state_root=state.state_root(),
    )


def _transfer(state: ChainState, frm: int, to: int, amount: int, events: List[Event]) -> None:
    if amount < 0:
        raise ValueOutOfRange("negative amount")
    src = state.account(frm)
    if src.balance < amount:
        raise InsufficientBalance(f"{frm} balance {src.balance} < {amount}")
    src.balance -= amount
    state.account(to).balance += amount
    events.append(Event("Transfer", {"from": frm, "to": to, "value": amount}))


def _transfer_from(
    state: ChainState,
    caller: int,
    frm: int,
    to: int,
    amount: int,
    events: List[Event],
) -> None:
    if amount < 0:
        raise ValueOutOfRange("negative amount")
    owner = state.account(frm)
    allowed = owner.allowances.get(caller, 0)
    if caller != frm and allowed < amount:
        raise InsufficientAllowance(f"allowance {allowed} < {amount}")
    if owner.balance < amount:
        raise InsufficientBalance(f"{frm} balance {owner.balance} < {amount}")
    owner.balance -= amount
    state.account(to).balance += amount
    if caller != frm:
        owner.allowances[caller] = allowed - amount
    events.append(Event("Transfer", {"from": frm, "to": to, "value": amount}))
