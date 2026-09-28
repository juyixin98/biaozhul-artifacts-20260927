"""Deterministic synthetic fixtures.

Generates a reproducible mix of valid and deliberately-malformed transactions
locally. No real accounts or network: private keys are derived from labelled
seeds via Keccak.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from ..abi import function_selector
from ..chain import (
    ChainState,
    Transaction,
    build_transaction,
    make_bootstrap_state,
)
from ..crypto import Signature, keccak256, privkey_address, sign_digest

ALICE_LABEL = "alice"
BOB_LABEL = "bob"
CAROL_LABEL = "carol"


@dataclass
class Actors:
    alice: int
    bob: int
    carol: int
    keys: Dict[int, bytes]

    def key_for(self, addr: int) -> bytes:
        return self.keys[addr]


def make_actors(seed: int) -> Actors:
    keys: Dict[int, bytes] = {}
    addrs: Dict[str, int] = {}
    for label in (ALICE_LABEL, BOB_LABEL, CAROL_LABEL):
        priv = keccak256(f"abibackend:seed:{seed}:{label}".encode())
        addr = int.from_bytes(privkey_address(priv), "big")
        keys[addr] = priv
        addrs[label] = addr
    return Actors(alice=addrs[ALICE_LABEL], bob=addrs[BOB_LABEL], carol=addrs[CAROL_LABEL], keys=keys)


def bootstrap(chain_id: int, actors: Actors) -> ChainState:
    return make_bootstrap_state(
        chain_id,
        {
            actors.alice: 1_000 * 10**18,
            actors.bob: 100 * 10**18,
            actors.carol: 0,
        },
    )


def _signed(
    actors: Actors, chain_id: int, sender: int, nonce: int, call: str,
    args: List[object], gas_price: int = 1,
) -> Transaction:
    return build_transaction(
        chain_id=chain_id,
        nonce=nonce,
        sender=sender,
        call=call,
        args=args,
        gas_price=gas_price,
        privkey=actors.key_for(sender),
    )


def build_scenario(chain_id: int, actors: Actors) -> List[Transaction]:
    """An ordered list mixing successes and expected failures (all deterministically signed)."""
    a, b, c = actors.alice, actors.bob, actors.carol
    txs: List[Transaction] = []

    # 1 valid transfer
    txs.append(_signed(actors, chain_id, a, 0, "transfer", [b, 40 * 10**18]))
    # 2 approve + 3 transferFrom (carol pulls from alice)
    txs.append(_signed(actors, chain_id, a, 1, "approve", [c, 10 * 10**18]))
    txs.append(_signed(actors, chain_id, c, 0, "transferFrom", [a, c, 10 * 10**18]))
    # 4 nonce gap: alice expected nonce 2, submits 9 -> expected failure
    txs.append(_signed(actors, chain_id, a, 9, "transfer", [b, 1]))
    # 5 insufficient balance: carol has 10, sends 9_999 -> expected failure
    txs.append(_signed(actors, chain_id, c, 1, "transfer", [b, 9_999 * 10**18]))
    # 6 resume alice at correct nonce 2 (zero amount)
    txs.append(_signed(actors, chain_id, a, 2, "transfer", [b, 0]))
    # 7 tampered calldata with a stale signature -> bad_signature
    good = _signed(actors, chain_id, a, 3, "transfer", [b, 1])
    tampered = Transaction(
        chain_id=good.chain_id,
        nonce=good.nonce,
        sender=good.sender,
        calldata=good.calldata[:-1] + bytes([good.calldata[-1] ^ 0x01]),
        gas_price=good.gas_price,
        signature=good.signature,
    )
    txs.append(tampered)
    # 8 truncated calldata, signed over the truncated bytes -> valid signature,
    #    but ABI decode is out-of-bounds -> invalid_calldata(offset_out_of_bounds)
    full_selector = function_selector("transfer", ["address", "uint256"])
    trunc_args = (
        (b & ((1 << 160) - 1)).to_bytes(32, "big")  # address word only; amount missing
    )
    trunc_calldata = full_selector + trunc_args
    trunc_tx = Transaction(
        chain_id=chain_id, nonce=4, sender=a, calldata=trunc_calldata,
        gas_price=1, signature=Signature(0, 0, 0),
    )
    trunc_tx.signature = sign_digest(actors.key_for(a), trunc_tx.signing_hash())
    txs.append(trunc_tx)
    # 9 resume alice at the still-expected nonce 4 with a valid transfer
    txs.append(_signed(actors, chain_id, a, 4, "transfer", [c, 5 * 10**18]))

    return txs
