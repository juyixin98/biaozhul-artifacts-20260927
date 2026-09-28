"""Chain-state kernel: pure transaction/block validation, no I/O.

The kernel takes a structurally decoded :class:`~utxo_ledger.encoding.Block`
plus a read-only :class:`ChainView` of committed state and returns an immutable
:class:`BlockEffect` describing exactly which UTXOs to spend/create. It never
mutates the view and never touches SQLite -- storage applies the returned effect
atomically (see :mod:`utxo_ledger.storage`). This split is what makes
"invalid block => nothing committed" enforceable: there is nothing to roll back
inside the kernel, and storage wraps application in one transaction.

Semantics (see README for the boundary contract):

* amounts are unsigned integers in ``[0, MAX_MONEY]``; every sum uses a checked
  u64 addition and raises ``VALUE_OVERFLOW`` instead of wrapping;
* a referenced output must be (a) committed-unspent, or (b) created by an
  earlier transaction in the same block;
* referencing a later/self position is rejected (``FORWARD_REFERENCE`` /
  ``CYCLIC_REFERENCE``) -- with strict backward ordering a multi-transaction
  cycle cannot be assembled, so the only remaining cycle shape is self-reference;
* the same outpoint may not appear twice within one block
  (``DUPLICATE_INPUT``); spending a committed-but-already-spent output is
  ``ALREADY_SPENT``; referencing something that never existed is
  ``UNKNOWN_OUTPOINT``;
* every input must carry an Ed25519 signature valid under the pubkey of the
  output it spends, over the canonical transaction sighash;
* conservation: for ordinary transactions ``sum(inputs) >= sum(outputs)`` and
  the difference is the fee; the coinbase (tx index 0, one null input whose
  vout equals the block height) may claim exactly ``BLOCK_SUBSIDY + fees``;
* zero-value outputs are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .crypto import verify_signature
from .encoding import (
    Block,
    Outpoint,
    Transaction,
    check_supported_versions,
    compute_block_hash,
    tx_sighash,
)
from .errors import ErrorCode, LedgerError
from .protocol import (
    BLOCK_SUBSIDY,
    GENESIS_HEIGHT,
    MAX_BLOCK_SIGOPS,
    MAX_MONEY,
    ZERO_HASH,
)


# --------------------------------------------------------------------------- #
# View contract
# --------------------------------------------------------------------------- #
class CommittedStatus(str, Enum):
    SPENT = "spent"


SPENT = CommittedStatus.SPENT


@dataclass(frozen=True)
class Utxo:
    value: int
    pubkey: bytes
    created_height: int


class ChainView(Protocol):
    """Read-only committed-state surface required by the kernel."""

    @property
    def tip_height(self) -> int: ...

    @property
    def tip_hash(self) -> bytes: ...

    def lookup_committed(self, outpoint: Outpoint):
        """Return ``Utxo`` if live, ``SPENT`` if it existed but is spent,
        or ``None`` if it never existed."""
        ...


# --------------------------------------------------------------------------- #
# Effects returned on success
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TxEffect:
    index: int
    tx: Transaction
    txid: bytes
    fee: int
    spent: tuple[Outpoint, ...]
    created: tuple[tuple[Outpoint, Utxo], ...]


@dataclass(frozen=True)
class BlockEffect:
    block: Block
    height: int
    block_hash: bytes
    prev_hash: bytes
    subsidy: int
    fees_total: int
    tx_effects: tuple[TxEffect, ...]

    @property
    def coinbase_claim(self) -> int:
        return sum(o.value for o in self.block.transactions[0].outputs)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def checked_add(a: int, b: int, what: str, tx_index: int | None = None) -> int:
    if a < 0 or b < 0 or a > MAX_MONEY or b > MAX_MONEY or a > MAX_MONEY - b:
        raise LedgerError(
            ErrorCode.VALUE_OVERFLOW,
            f"u64 overflow while summing {what}: {a} + {b}",
            tx_index=tx_index,
        )
    return a + b


def _is_null_outpoint(op: Outpoint) -> bool:
    return op.txid == ZERO_HASH


@dataclass
class _Pending:
    """Intra-block overlay while walking transactions in order."""

    created: dict[bytes, tuple[Utxo, ...]]      # txid -> outputs
    positions: dict[bytes, int]                 # txid -> block index
    spent_in_block: set[tuple[bytes, int]]      # outpoints consumed so far
    fees: int = 0
    sigops: int = 0
    steps: int = 0
    # Observability only: sink("tx_ok"|"tx_fail", tx_index, txid, info-dict).
    sink: object = lambda *a, **k: None  # noqa: E731

    def tick(self) -> None:
        # Defensive loop guard; normal blocks stay orders of magnitude below.
        self.steps += 1
        if self.steps > self.max_steps:
            raise LedgerError(
                ErrorCode.BLOCK_VALIDATION_STEPS_EXCEEDED,
                f"exceeded {self.max_steps} validation steps",
            )


_Pending.max_steps = 1_000_000  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_block(
    block: Block,
    view: ChainView,
    sink: "callable | None" = None,
) -> BlockEffect:
    """Validate a whole block; returns effects or raises :class:`LedgerError`.

    On failure no effect is returned and the caller's committed state is
    untouched (this function has no write surface).

    ``sink`` is an optional observability callback receiving
    ``sink("tx_ok"|"tx_fail", tx_index, txid, info)``; callers must pass a
    callback that never raises (the node's logger swallows its own I/O errors).
    """
    check_supported_versions(block)

    txs = block.transactions
    if not txs:
        raise LedgerError(ErrorCode.EMPTY_BLOCK, "block contains no transactions")

    if block.height != view.tip_height + 1 or view.tip_height < GENESIS_HEIGHT:
        raise LedgerError(
            ErrorCode.BLOCK_HEIGHT_INVALID,
            f"block height {block.height} does not extend tip height "
            f"{view.tip_height}",
            details={"got": block.height, "expected": view.tip_height + 1},
        )
    if block.prev_hash != view.tip_hash:
        raise LedgerError(
            ErrorCode.PREV_BLOCK_HASH_MISMATCH,
            "prev_hash does not match the current chain tip",
            details={"got": block.prev_hash.hex(), "expected": view.tip_hash.hex()},
        )

    pending = _Pending(created={}, positions={}, spent_in_block=set())
    pending.sink = sink or (lambda *a, **k: None)

    # Index txids and reject duplicates before resolving anything.
    for i, tx in enumerate(txs):
        if tx.txid in pending.positions:
            raise LedgerError(
                ErrorCode.DUPLICATE_TXID,
                f"txid {tx.txid.hex()} appears at positions "
                f"{pending.positions[tx.txid]} and {i}",
                tx_index=i,
            )
        pending.positions[tx.txid] = i

    effects: list[TxEffect] = []
    for i, tx in enumerate(txs):
        try:
            if i == 0:
                cb = _validate_coinbase(tx, block.height, i)
                effects.append(cb)
                pending.created[tx.txid] = tuple(u for _, u in cb.created)
                pending.sink(
                    "tx_ok", i, tx.txid,
                    {"kind": "coinbase", "claim": sum(o.value for o in tx.outputs)},
                )
            else:
                effect = _validate_tx(tx, i, view, pending)
                effects.append(effect)
                pending.sink(
                    "tx_ok", i, tx.txid,
                    {"kind": "transfer", "fee": effect.fee},
                )
        except LedgerError as exc:
            pending.sink(
                "tx_fail", i, tx.txid,
                {
                    "code": exc.code.value,
                    "category": exc.category.value,
                    "reason": exc.message,
                },
            )
            raise

    subsidy = BLOCK_SUBSIDY
    total_claim_limit = checked_add(subsidy, pending.fees, "subsidy + fees")
    coinbase = txs[0]
    claim = 0
    for out in coinbase.outputs:
        claim = checked_add(claim, out.value, "coinbase output amounts", tx_index=0)
    if claim != total_claim_limit:
        raise LedgerError(
            ErrorCode.BAD_COINBASE,
            f"coinbase claims {claim} but subsidy+fees is {total_claim_limit}",
            tx_index=0,
            details={
                "claimed": claim,
                "subsidy": subsidy,
                "fees": pending.fees,
                "allowed": total_claim_limit,
            },
        )

    return BlockEffect(
        block=block,
        height=block.height,
        block_hash=compute_block_hash(block),
        prev_hash=block.prev_hash,
        subsidy=subsidy,
        fees_total=pending.fees,
        tx_effects=tuple(effects),
    )


def _validate_coinbase(tx: Transaction, height: int, index: int) -> TxEffect:
    if len(tx.inputs) != 1 or not _is_null_outpoint(tx.inputs[0].outpoint):
        raise LedgerError(
            ErrorCode.BAD_COINBASE,
            "first transaction must be the coinbase with one null input",
            tx_index=index,
        )
    op = tx.inputs[0].outpoint
    if op.vout != height:
        raise LedgerError(
            ErrorCode.BAD_COINBASE,
            f"coinbase height field {op.vout} != block height {height}",
            tx_index=index,
            details={"got": op.vout, "expected": height},
        )
    if tx.inputs[0].signature != b"":
        raise LedgerError(
            ErrorCode.BAD_COINBASE,
            "coinbase input must not carry a signature",
            tx_index=index,
        )
    created: list[tuple[Outpoint, Utxo]] = []
    for vout, out in enumerate(tx.outputs):
        if out.value <= 0:
            raise LedgerError(
                ErrorCode.ZERO_VALUE_OUTPUT,
                "zero-value output in coinbase",
                tx_index=index,
            )
        if out.value > MAX_MONEY:
            raise LedgerError(
                ErrorCode.VALUE_OUT_OF_RANGE,
                f"output value {out.value} exceeds MAX_MONEY",
                tx_index=index,
            )
        created.append(
            (Outpoint(tx.txid, vout), Utxo(out.value, out.pubkey, height))
        )
    return TxEffect(index, tx, tx.txid, 0, (), tuple(created))


def _resolve_outpoint(
    op: Outpoint,
    tx_index: int,
    view: ChainView,
    pending: _Pending,
    spend_txid: bytes,
) -> Utxo:
    pending.tick()

    # Intra-block reference ordering.
    if op.txid in pending.positions:
        j = pending.positions[op.txid]
        if op.txid == spend_txid or j == tx_index:
            raise LedgerError(
                ErrorCode.CYCLIC_REFERENCE,
                f"tx[{tx_index}] references its own output vout={op.vout}",
                tx_index=tx_index,
                outpoint=op.key(),
            )
        if j > tx_index:
            raise LedgerError(
                ErrorCode.FORWARD_REFERENCE,
                f"tx[{tx_index}] spends output of later tx[{j}]",
                tx_index=tx_index,
                outpoint=op.key(),
            )

    key = op.key()
    if key in pending.spent_in_block:
        raise LedgerError(
            ErrorCode.DOUBLE_SPEND,
            f"outpoint {op.txid.hex()}:{op.vout} already spent by an "
            "earlier transaction in this block",
            tx_index=tx_index,
            outpoint=key,
        )

    if op.txid in pending.created:
        outputs = pending.created[op.txid]
        if not (0 <= op.vout < len(outputs)):
            raise LedgerError(
                ErrorCode.UNKNOWN_OUTPOINT,
                f"vout {op.vout} does not exist in intra-block tx "
                f"{op.txid.hex()}",
                tx_index=tx_index,
                outpoint=key,
            )
        pending.spent_in_block.add(key)
        return outputs[op.vout]

    committed = view.lookup_committed(op)
    if committed is SPENT:
        raise LedgerError(
            ErrorCode.ALREADY_SPENT,
            f"outpoint {op.txid.hex()}:{op.vout} was already spent",
            tx_index=tx_index,
            outpoint=key,
        )
    if committed is None:
        # Distinguish "refers to a tx that exists later in this block" was
        # handled above; here the funding tx is simply unknown.
        raise LedgerError(
            ErrorCode.UNKNOWN_OUTPOINT,
            f"unknown or unconfirmed outpoint {op.txid.hex()}:{op.vout}",
            tx_index=tx_index,
            outpoint=key,
        )
    pending.spent_in_block.add(key)
    return committed


def _validate_tx(
    tx: Transaction,
    index: int,
    view: ChainView,
    pending: _Pending,
) -> TxEffect:
    if len(tx.inputs) == 1 and _is_null_outpoint(tx.inputs[0].outpoint):
        raise LedgerError(
            ErrorCode.BAD_COINBASE,
            "only the first transaction may be a coinbase (null input)",
            tx_index=index,
        )

    local_seen: set[tuple[bytes, int]] = set()
    sources: list[Utxo] = []
    spent: list[Outpoint] = []
    sighash = tx_sighash(tx)

    in_sum = 0
    for inp in tx.inputs:
        pending.tick()
        key = inp.outpoint.key()
        if key in local_seen:
            raise LedgerError(
                ErrorCode.DUPLICATE_INPUT,
                f"same outpoint {inp.outpoint.txid.hex()}:{inp.outpoint.vout} "
                f"listed twice in one transaction",
                tx_index=index,
                outpoint=key,
            )
        local_seen.add(key)
        source = _resolve_outpoint(inp.outpoint, index, view, pending, tx.txid)
        # Signature: fixed Ed25519 over the canonical sighash.
        pending.sigops += 1
        if pending.sigops > MAX_BLOCK_SIGOPS:
            raise LedgerError(
                ErrorCode.TOO_MANY_SIGOPS,
                f"block exceeds {MAX_BLOCK_SIGOPS} signature checks",
                tx_index=index,
            )
        try:
            verify_signature(source.pubkey, sighash, inp.signature)
        except LedgerError as exc:
            if exc.tx_index is None:
                raise LedgerError(
                    exc.code,
                    exc.message,
                    tx_index=index,
                    outpoint=exc.outpoint,
                    details=exc.details,
                ) from exc
            raise
        in_sum = checked_add(in_sum, source.value, "input amounts", index)
        sources.append(source)
        spent.append(inp.outpoint)

    out_sum = 0
    created: list[tuple[Outpoint, Utxo]] = []
    for vout, out in enumerate(tx.outputs):
        pending.tick()
        if out.value <= 0:
            raise LedgerError(
                ErrorCode.ZERO_VALUE_OUTPUT,
                f"output {vout} has zero/negative value {out.value}",
                tx_index=index,
            )
        if out.value > MAX_MONEY:
            raise LedgerError(
                ErrorCode.VALUE_OUT_OF_RANGE,
                f"output value {out.value} exceeds MAX_MONEY",
                tx_index=index,
            )
        out_sum = checked_add(out_sum, out.value, "output amounts", index)
        created.append(
            (
                Outpoint(tx.txid, vout),
                Utxo(out.value, out.pubkey, view.tip_height + 1),
            )
        )

    if out_sum > in_sum:
        raise LedgerError(
            ErrorCode.FEE_NEGATIVE,
            f"transaction spends {in_sum} but creates {out_sum} "
            "(conservation violated)",
            tx_index=index,
            details={"input_sum": in_sum, "output_sum": out_sum},
        )
    fee = in_sum - out_sum

    pending.fees = checked_add(pending.fees, fee, "block fees", index)
    pending.created[tx.txid] = tuple(u for _, u in created)
    return TxEffect(index, tx, tx.txid, fee, tuple(spent), tuple(created))


# --------------------------------------------------------------------------- #
# Reference in-memory view (also used by the node when SQLite is not required)
# --------------------------------------------------------------------------- #
class InMemoryChainView:
    """Deterministic in-memory committed state, implementing :class:`ChainView`."""

    def __init__(self) -> None:
        self._utxos: dict[tuple[bytes, int], Utxo] = {}
        self._spent: set[tuple[bytes, int]] = set()
        self._tip_height = GENESIS_HEIGHT
        self._tip_hash = ZERO_HASH

    @property
    def tip_height(self) -> int:
        return self._tip_height

    @property
    def tip_hash(self) -> bytes:
        return self._tip_hash

    def lookup_committed(self, op: Outpoint):
        key = op.key()
        if key in self._utxos:
            return self._utxos[key]
        if key in self._spent:
            return SPENT
        return None

    # -- mutation hooks (used by the node/tests, not the kernel) -------------
    def apply_block(self, effect: BlockEffect) -> None:
        for te in effect.tx_effects:
            for op in te.spent:
                if op.key() not in self._utxos:
                    raise LedgerError(
                        ErrorCode.INTERNAL_ERROR,
                        "in-memory apply: spending unknown utxo",
                    )
                del self._utxos[op.key()]
                self._spent.add(op.key())
            for op, utxo in te.created:
                self._utxos[op.key()] = utxo
        self._tip_height = effect.height
        self._tip_hash = effect.block_hash

    def snapshot_utxos(self) -> dict[tuple[str, int], dict]:
        return {
            (txid.hex(), vout): {
                "value": u.value,
                "pubkey": u.pubkey.hex(),
                "height": u.created_height,
            }
            for (txid, vout), u in sorted(self._utxos.items(), key=lambda kv: (kv[0][0], kv[0][1]))
        }

    def utxo_count(self) -> int:
        return len(self._utxos)
