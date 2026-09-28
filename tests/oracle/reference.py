"""Independent reference oracle for the review tests.

This module deliberately imports **none** of the packages under test
(``reorgindex.kernel`` / ``reorgindex.storage``).  It re-derives what the best
chain and the ledger projection must be using plain dicts and the *raw fixture
blocks*, so agreement with the engine is a real cross-check rather than the
system testing against its own outputs.

It does reuse the canonical hashing spec via the crypto sub-package
(serialization is a wire-format definition, not consensus logic); the Merkle
root and identity hash are independently recomputed here with local code.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _canon(obj: object) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


HEADER_KEYS = (
    "version", "height", "parent", "merkle_root", "difficulty",
    "timestamp", "producer", "nonce",
)
TX_KEYS = ("type", "nonce", "sender", "recipient", "amount", "fee", "fee_recipient")


def oracle_txid(tx: dict) -> str:
    return _sha(_canon({k: tx[k] for k in TX_KEYS}))


def oracle_merkle(txids: list[str]) -> str:
    if not txids:
        return "0" * 64
    level = list(txids)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            _sha(bytes.fromhex(level[i]) + bytes.fromhex(level[i + 1]))
            for i in range(0, len(level), 2)
        ]
    return level[0]


def oracle_block_hash(block: dict) -> str:
    return _sha(_canon({k: block[k] for k in HEADER_KEYS}))


@dataclass
class OracleBlock:
    name: str
    hash: str
    height: int
    parent: str
    weight: int
    payload: dict


@dataclass
class OracleLedger:
    balances: dict[str, int] = field(default_factory=dict)
    nonces: dict[str, int] = field(default_factory=dict)
    txids: set[str] = field(default_factory=set)
    # ordered event tuples: (txid, kind, address, amount_delta, nonce_delta)
    events: list[tuple] = field(default_factory=list)

    def apply(self, block: OracleBlock) -> None:
        for pos, tx in enumerate(block.payload["transactions"]):
            assert oracle_txid(tx) == tx["txid"], "oracle: txid mismatch in fixture"
            assert tx["txid"] not in self.txids, "oracle: duplicate txid on one chain"
            self.txids.add(tx["txid"])
            amount = int(tx["amount"])
            fee = int(tx["fee"])
            if tx["type"] == "mint":
                assert block.height == 0
                self.balances[tx["recipient"]] = self.balances.get(tx["recipient"], 0) + amount
                self.events.append((tx["txid"], "credit", tx["recipient"], amount, 0))
                continue
            assert block.height > 0 and tx["type"] == "transfer"
            sender = tx["sender"]
            expected_nonce = self.nonces.get(sender, 0) + 1
            assert tx["nonce"] == expected_nonce, (
                f"oracle: nonce {tx['nonce']} != expected {expected_nonce}"
            )
            have = self.balances.get(sender, 0)
            assert have >= amount + fee, "oracle: insufficient funds"
            self.balances[sender] = have - amount - fee
            self.nonces[sender] = expected_nonce
            self.balances[tx["recipient"]] = self.balances.get(tx["recipient"], 0) + amount
            self.events.append((tx["txid"], "debit", sender, -(amount + fee), 1))
            self.events.append((tx["txid"], "credit", tx["recipient"], amount, 0))
            if fee:
                fr = tx["fee_recipient"]
                self.balances[fr] = self.balances.get(fr, 0) + fee
                self.events.append((tx["txid"], "fee", fr, fee, 0))


def load_blocks(recording: dict) -> dict[str, OracleBlock]:
    out: dict[str, OracleBlock] = {}
    for entry in recording["blocks"]:
        b = entry["block"]
        assert b["merkle_root"] == oracle_merkle([t["txid"] for t in b["transactions"]])
        out[entry["name"]] = OracleBlock(
            name=entry["name"],
            hash=oracle_block_hash(b),
            height=int(b["height"]),
            parent=b["parent"],
            weight=int(b["difficulty"]),
            payload=b,
        )
    # check parent hashes
    for blk in out.values():
        if blk.height > 0:
            parent_names = [n for n, x in out.items() if x.hash == blk.parent]
            assert parent_names, f"oracle: block {blk.name} parent not in fixture"
    return out


def chain_names_to_tip(tip_name: str, blocks: dict[str, OracleBlock]) -> list[str]:
    by_hash = {b.hash: b for b in blocks.values()}
    out: list[str] = []
    cur = blocks[tip_name]
    while cur.height > 0:
        out.append(cur.name)
        cur = by_hash[cur.parent]
    out.append(cur.name)  # genesis
    out.reverse()
    return out


def chain_weight(names: list[str], blocks: dict[str, OracleBlock]) -> int:
    return sum(blocks[n].weight for n in names)


def expected_projection(
    recording: dict,
    *,
    tip_name: str,
) -> dict:
    """Replay genesis..tip and return balances, nonces, events, txids."""
    blocks = load_blocks(recording)
    names = chain_names_to_tip(tip_name, blocks)
    ledger = OracleLedger()
    for n in names:
        ledger.apply(blocks[n])
    return {
        "tip_name": tip_name,
        "chain_names": names,
        "chain_hashes": [blocks[n].hash for n in names],
        "cumulative_weight": chain_weight(names, blocks),
        "balances": dict(ledger.balances),
        "nonces": dict(ledger.nonces),
        "events": list(ledger.events),
        "txids": set(ledger.txids),
    }


@dataclass
class ArrivalDecision:
    name: str
    outcome: str           # ACCEPT_EXTEND | ACCEPT_SWITCH | ACCEPT_FORK | PENDING | REJECTED
    reason: Optional[str] = None
    active_tip_after: Optional[str] = None
    rollback_range: Optional[list[int]] = None


def simulate_arrivals(
    recording: dict,
    *,
    finality_depth: int,
    tie_prefers_lower_hash: bool = True,
) -> dict[str, ArrivalDecision]:
    """Reference fork-choice simulation over the declared arrival order.

    Returns one decision per arrival *name*.  Orphans suspend and are later
    released when their parent becomes the active tip; a released block's
    entry is replaced with its post-release decision so callers see the final
    disposition per block.
    """
    blocks = load_blocks(recording)
    active_tip: Optional[str] = None
    stored: set[str] = set()
    pending: dict[str, str] = {}  # block name -> parent hash
    decisions: dict[str, ArrivalDecision] = {}

    def tip_weight() -> int:
        return chain_weight(chain_names_to_tip(active_tip, blocks), blocks)

    def release_cascade(anchor: str) -> None:
        progress = True
        while progress:
            progress = False
            for name, parent_hash in list(pending.items()):
                # Release only when the parent is the current ACTIVE TIP: the
                # engine only auto-extends the tip; a pending block whose
                # parent sits deeper in the active chain would instead be
                # evaluated as a fork, not released from suspension.
                if blocks[active_tip].hash != parent_hash:
                    continue
                del pending[name]
                store_and_choose(name)
                progress = True
                break

    def store_and_choose(name: str) -> None:
        nonlocal active_tip
        blk = blocks[name]
        stored.add(name)
        if blk.height == 0:
            active_tip = name
            decisions[name] = ArrivalDecision(name, "ACCEPT_EXTEND", active_tip_after=name)
            return
        # Is the parent on the current active chain?
        active_names = chain_names_to_tip(active_tip, blocks)
        active_hashes = {blocks[n].hash for n in active_names}
        parent_is_active = blk.parent in active_hashes
        if parent_is_active and blocks[active_tip].hash == blk.parent:
            active_tip = name
            decisions[name] = ArrivalDecision(name, "ACCEPT_EXTEND", active_tip_after=name)
            return
        # Evaluate candidate chain weight against active.
        cand_names = chain_names_to_tip(name, blocks)
        cand_ancestors_present = all(
            n in stored or n == name for n in cand_names
        )
        if not parent_is_active:
            # parent may be a stored non-active block; candidate complete?
            if not cand_ancestors_present:
                pending[name] = blk.parent
                decisions[name] = ArrivalDecision(name, "PENDING", active_tip_after=active_tip)
                return
        cw = chain_weight(cand_names, blocks)
        aw = tip_weight()
        wins = cw > aw or (
            cw == aw
            and tie_prefers_lower_hash
            and blk.hash < blocks[active_tip].hash
        )
        if not wins:
            decisions[name] = ArrivalDecision(name, "ACCEPT_FORK", active_tip_after=active_tip)
            return
        # divergence
        old_names = chain_names_to_tip(active_tip, blocks)
        common = [n for n in cand_names if n in old_names]
        first_new_idx = len(common)
        detach = old_names[first_new_idx:]
        shallow = blocks[detach[0]]
        old_tip = blocks[active_tip]
        confirmations = old_tip.height - shallow.height + 1
        if confirmations > finality_depth:
            decisions[name] = ArrivalDecision(
                name,
                "REJECTED",
                reason="REORG_FINALIZED",
                active_tip_after=active_tip,
                rollback_range=[shallow.height, old_tip.height],
            )
            return
        decisions[name] = ArrivalDecision(
            name,
            "ACCEPT_SWITCH",
            active_tip_after=name,
            rollback_range=[shallow.height, old_tip.height],
        )
        active_tip = name

    for name in recording["arrival_order"]:
        blk = blocks[name]
        if blk.height > 0:
            known_hashes = {blocks[n].hash for n in stored}
            if blk.parent not in known_hashes:
                pending[name] = blk.parent
                decisions[name] = ArrivalDecision(name, "PENDING", active_tip_after=active_tip)
                continue
        store_and_choose(name)
        release_cascade(name)

    return decisions
