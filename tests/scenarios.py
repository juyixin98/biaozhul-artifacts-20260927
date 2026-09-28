"""Shared scenario construction for kernel/node tests.

A :class:`Scenario` runs the same setup+target blocks through:

* the production stack (decode -> kernel -> :class:`SqliteStore`), and
* the independent :mod:`tests.oracle`,

and exposes both snapshots so tests can assert concrete outcomes and compare
against the reference implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from utxo_ledger.encoding import Block, encode_block
from utxo_ledger.node import LedgerNode
from utxo_ledger.protocol import BLOCK_SUBSIDY
from utxo_ledger.storage import SqliteStore

from . import oracle
from .fixtures import FixtureBuilder, coinbase_tx


@dataclass
class ScenarioResult:
    prod_result: object
    oracle_error: dict | None
    prod_snapshot: dict
    oracle_snapshot: dict
    block: Block
    raw: bytes
    oracle_verdict: object | None = None
    prod_intermediate: list[dict] = field(default_factory=list)
    intermediate_match: bool = False


def _normalize_coinbase(snapshot: dict, coinbase_txid: str) -> dict:
    """Map coinbase slots to a canonical key for cross-implementation comparison.

    Prefix replays rebuild the coinbase with a claim equal to
    subsidy + fees-so-far, giving it a different txid than the full block's
    coinbase. Every other outpoint keeps its real key and must match exactly.
    """
    return {
        (("COINBASE", vout) if txid == coinbase_txid else (txid, vout)): entry
        for (txid, vout), entry in snapshot.items()
    }


class Scenario:
    def __init__(self) -> None:
        self.fb = FixtureBuilder()
        self.store = SqliteStore(":memory:")
        self.node = LedgerNode(self.store)
        self.ost = oracle.OracleState()
        self.setup_raw: list[bytes] = []
        self._seq = 0

    def append(self, txs, *, height: int | None = None) -> Block:
        block = self.fb.append(list(txs), height=height)
        raw = encode_block(block)
        self.setup_raw.append(raw)
        self._seq += 1
        pres = self.node.submit_raw_block(raw, self._seq)
        assert pres.accepted, f"setup block rejected: {pres.error}"
        over = oracle.apply_block_to_state(raw, self.ost)
        assert over.accepted, f"oracle rejected valid setup: {over.error}"
        assert self.store.snapshot_utxos() == self.ost.snapshot(), (
            "production/reference divergence during setup"
        )
        return block

    def run_target(self, txs, *, height: int | None = None) -> ScenarioResult:
        h = height if height is not None else self.fb.height + 1
        block = Block(1, h, self.fb.prev_hash, tuple(txs))
        raw = encode_block(block)
        self._seq += 1
        pres = self.node.submit_raw_block(raw, self._seq)

        oracle_error = None
        verdict = None
        ref = self.ost.copy()
        try:
            verdict = oracle.apply_block_to_state(raw, ref)
        except oracle.OracleError as exc:
            oracle_error = exc.to_dict()

        result = ScenarioResult(
            prod_result=pres,
            oracle_error=oracle_error,
            prod_snapshot=self.store.snapshot_utxos(),
            oracle_snapshot=ref.snapshot(),
            block=block,
            raw=raw,
            oracle_verdict=verdict,
        )

        # On success, compare EVERY per-transaction intermediate reference
        # state against an independent production replay of each block prefix.
        if pres.accepted and verdict is not None:
            prod_mid, prod_cb = self._production_intermediate_states(
                block, verdict.fees_by_tx
            )
            ref_mid, ref_cb = self._oracle_intermediate_states(
                block, verdict.fees_by_tx
            )
            prod_norm = [
                _normalize_coinbase(s, cb) for s, cb in zip(prod_mid, prod_cb)
            ]
            ref_norm = [
                _normalize_coinbase(s, cb) for s, cb in zip(ref_mid, ref_cb)
            ]
            result.prod_intermediate = prod_mid
            result.intermediate_match = prod_norm == ref_norm
        return result

    def _oracle_intermediate_states(self, block: Block, fees_by_tx: dict):
        """Independent oracle replay of each prefix on a fresh OracleState.

        Mirrors :meth:`_production_intermediate_states` so the comparison is
        prefix-by-prefix against different code rather than against the full
        block's overlay.
        """
        from .fixtures import coinbase_tx as cb_builder

        txs = list(block.transactions)
        snaps: list[dict] = []
        cb_txids: list[str] = []
        for k in range(1, len(txs) + 1):
            ref = self.ost.copy()
            prefix_fee = sum(fees_by_tx.get(i, 0) for i in range(1, k))
            cb_pk = txs[0].outputs[0].pubkey
            cb = cb_builder(block.height, [(BLOCK_SUBSIDY + prefix_fee, cb_pk)])
            # Re-encode the prefix using production's Block container only as a
            # wire vehicle; all *answers* come from the oracle parser/validator.
            prefix = Block(
                block.version, block.height, block.prev_hash,
                (cb, *txs[1:k]),
            )
            over = oracle.apply_block_to_state(encode_block(prefix), ref)
            assert over.accepted, f"oracle prefix {k} rejected: {over.error}"
            snaps.append(ref.snapshot())
            cb_txids.append(cb.txid.hex())
        return snaps, cb_txids

    def _production_intermediate_states(self, block: Block, fees_by_tx: dict):
        """Replay each prefix [coinbase .. tx[k]] on a fresh SQLite store.

        Returns ``(snapshots, coinbase_txids)`` where the latter is the coinbase
        txid used in each prefix replay (they differ per prefix because the
        coinbase claim is subsidy + fees-so-far).
        """
        txs = list(block.transactions)
        snaps: list[dict] = []
        cb_txids: list[str] = []
        for k in range(1, len(txs) + 1):
            store = SqliteStore(":memory:")
            node = LedgerNode(store)
            for raw in self.setup_raw:
                r = node.submit_raw_block(raw, 0)
                assert r.accepted, r.error
            prefix_fee = sum(fees_by_tx.get(i, 0) for i in range(1, k))
            cb_pk = txs[0].outputs[0].pubkey
            cb = coinbase_tx(block.height, [(BLOCK_SUBSIDY + prefix_fee, cb_pk)])
            prefix = Block(
                block.version,
                block.height,
                block.prev_hash,
                (cb, *txs[1:k]),
            )
            r = node.submit_raw_block(encode_block(prefix), 0)
            assert r.accepted, (
                f"prefix through tx[{k - 1}] should be valid: {r.error}"
            )
            snaps.append(store.snapshot_utxos())
            cb_txids.append(cb.txid.hex())
            store.close()
        return snaps, cb_txids

    @property
    def tip_height(self) -> int:
        return self.store.tip_height

    @property
    def prev_hash(self) -> bytes:
        return self.fb.prev_hash
