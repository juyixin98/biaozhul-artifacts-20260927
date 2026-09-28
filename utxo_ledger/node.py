"""Node orchestration: decode -> kernel validation -> atomic storage commit.

This is the only layer that wires pure modules together. Given raw block bytes
it performs structural decoding (encoding), semantic validation (kernel) and a
single atomic write (storage). Structural errors are caught before the kernel
runs, so the error category correctly distinguishes "bad bytes" (INPUT /
RESOURCE) from "conflicts with state" (STATE) from machinery faults
(COMPUTATION).

Every attempt optionally emits structured events to a run log; the log records
the run id, pre/post UTXO-set fingerprints and the failing code+category, which
is enough to replay the exact problem afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .encoding import Block, decode_block
from .errors import LedgerError
from .kernel import BlockEffect, validate_block
from .runlog import RunLogger, utxo_set_fingerprint
from .storage import SqliteStore

Sink = Callable[..., None]


@dataclass(frozen=True)
class BlockAttemptResult:
    accepted: bool
    seq: int
    height: int | None
    block_hash: str | None
    fee_total: int | None
    error: dict | None
    snapshot_before: dict
    snapshot_after: dict
    raw_size: int

    @property
    def state_unchanged(self) -> bool:
        """True when the UTXO set after the attempt equals the set before it."""
        return (
            self.snapshot_before["sha256"] == self.snapshot_after["sha256"]
            and self.snapshot_before["count"] == self.snapshot_after["count"]
        )


class LedgerNode:
    def __init__(self, store: SqliteStore, logger: RunLogger | None = None) -> None:
        self.store = store
        self.logger = logger

    @property
    def tip(self) -> dict:
        return {"height": self.store.tip_height, "hash": self.store.tip_hash.hex()}

    def submit_raw_block(self, raw: bytes, seq: int) -> BlockAttemptResult:
        snap_before = utxo_set_fingerprint(self.store.snapshot_utxos())
        raw_size = len(raw)
        block: Block | None = None
        effect: BlockEffect | None = None

        def sink(event: str, tx_index: int, txid: bytes, info: dict[str, Any]) -> None:
            if self.logger is not None:
                try:
                    self.logger.tx_check(
                        seq,
                        tx_index,
                        stage="semantic",
                        verdict="ok" if event == "tx_ok" else "fail",
                        txid=txid.hex(),
                        code=info.get("code"),
                        category=info.get("category"),
                        reason=info.get("reason"),
                        **{k: v for k, v in info.items()
                           if k not in {"code", "category", "reason"}},
                    )
                except Exception:  # logging must never break consensus work
                    pass

        try:
            try:
                block = decode_block(raw)
            except LedgerError:
                raise
            if self.logger is not None:
                self.logger.block_start(
                    seq,
                    block.height,
                    raw_size,
                    [tx.txid.hex() for tx in block.transactions],
                    prev_hash=block.prev_hash.hex(),
                )
            effect = validate_block(block, self.store, sink=sink)
            self.store.apply_block(effect)
        except LedgerError as exc:
            snap_after = utxo_set_fingerprint(self.store.snapshot_utxos())
            if self.logger is not None:
                self.logger.block_result(
                    seq,
                    verdict="rejected",
                    height=block.height if block else None,
                    code=exc.code.value,
                    category=exc.category.value,
                    reason=exc.message,
                    snapshot_before=snap_before,
                    snapshot_after=snap_after,
                    raw_size=raw_size,
                    tx_index=exc.tx_index,
                )
            return BlockAttemptResult(
                accepted=False,
                seq=seq,
                height=block.height if block else None,
                block_hash=None,
                fee_total=None,
                error=exc.to_dict(),
                snapshot_before=snap_before,
                snapshot_after=snap_after,
                raw_size=raw_size,
            )

        snap_after = utxo_set_fingerprint(self.store.snapshot_utxos())
        if self.logger is not None:
            self.logger.block_result(
                seq,
                verdict="accepted",
                height=effect.height,
                block_hash=effect.block_hash.hex(),
                fee_total=effect.fees_total,
                snapshot_before=snap_before,
                snapshot_after=snap_after,
                raw_size=raw_size,
            )
        return BlockAttemptResult(
            accepted=True,
            seq=seq,
            height=effect.height,
            block_hash=effect.block_hash.hex(),
            fee_total=effect.fees_total,
            error=None,
            snapshot_before=snap_before,
            snapshot_after=snap_after,
            raw_size=raw_size,
        )
