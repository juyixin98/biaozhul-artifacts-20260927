"""Offline replay: feed a fixture sequence through kernel + storage.

No network. The driver reads a local JSON scenario (see ``fixtures/``), executes
every block in order, persists to SQLite and emits an explainable report that
separates accepted blocks, per-transaction failures and uncertainty warnings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..params import PARAMS
from ..kernel import Chain
from ..kernel.execution import ChainState
from ..kernel.models import Block
from ..storage import Store, StorageError
from ..api.wire import WireError, structured_to_transaction
from ..api.logging_setup import StructuredLogger, configure_logging, new_request_id


@dataclass
class ReplaySummary:
    scenario: str
    protocol_version: str
    blocks_total: int
    blocks_accepted: int
    blocks_rejected: int
    valid_txs: int
    invalid_txs: int
    total_burned: int
    total_tipped: int
    head_number: int
    head_next_base_fee: int
    failures: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    blocks: list[dict] = field(default_factory=list)


def load_scenario(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def replay_scenario(scenario: dict, *, db_path: str = ":memory:",
                    chain: Optional[Chain] = None,
                    store: Optional[Store] = None,
                    strict: Optional[bool] = None) -> ReplaySummary:
    configure_logging()
    log = StructuredLogger("replay")
    new_request_id()

    if scenario.get("protocol_version") not in (None, PARAMS.protocol_version):
        log.failure("protocol_mismatch", "scenario targets another protocol version",
                    expected=PARAMS.protocol_version,
                    got=scenario.get("protocol_version"))

    state = chain.state if chain is not None else ChainState(
        balances={a: int(v, 10) for a, v in scenario.get("genesis_balances", {}).items()}
    )
    chain = chain or Chain(state)
    store = store or Store(db_path)

    summary = ReplaySummary(
        scenario=scenario.get("scenario", "unnamed"),
        protocol_version=PARAMS.protocol_version,
        blocks_total=len(scenario.get("blocks", [])),
        blocks_accepted=0,
        blocks_rejected=0,
        valid_txs=0,
        invalid_txs=0,
        total_burned=0,
        total_tipped=0,
        head_number=chain.head.number,
        head_next_base_fee=chain.head.next_base_fee,
    )

    for b in scenario.get("blocks", []):
        block_strict = b.get("strict") if strict is None else strict
        # Resolve parent-hash placeholders from the live head so fixtures can
        # stay self-describing without precomputing hashes.
        parent_hash = b["parent_hash"]
        if isinstance(parent_hash, str) and parent_hash.startswith("<computed"):
            if int(b["number"]) == chain.head.number + 1:
                parent_hash = chain.head.block_hash
        try:
            txs = [structured_to_transaction(t) for t in b.get("transactions", [])]
        except WireError as exc:
            summary.blocks_rejected += 1
            summary.failures.append({
                "block_number": b.get("number"),
                "code": exc.code, "detail": exc.detail,
            })
            continue
        block = Block(
            number=int(b["number"]),
            parent_hash=parent_hash,
            base_fee_per_gas=int(b["base_fee_per_gas"], 10),
            gas_limit=int(b["gas_limit"]),
            gas_used=int(b["gas_used"]),
            transactions=txs,
        )
        executed = chain.apply_block(block, strict=bool(block_strict))
        if not executed.accepted:
            summary.blocks_rejected += 1
            summary.failures.append({
                "block_number": block.number,
                "code": executed.block_error,
                "detail": executed.block_error_detail,
            })
            log.failure("replay_block_rejected", executed.block_error_detail or "",
                        number=block.number, code=executed.block_error)
            continue
        try:
            store.save_executed(executed)
        except StorageError as exc:
            summary.blocks_rejected += 1
            summary.failures.append({
                "block_number": block.number,
                "code": exc.code, "detail": exc.detail,
            })
            continue

        summary.blocks_accepted += 1
        summary.valid_txs += sum(1 for r in executed.receipts if r.valid)
        summary.invalid_txs += len(executed.invalid)
        summary.total_burned += executed.total_burned
        summary.total_tipped += executed.total_tipped
        summary.head_number = block.number
        summary.head_next_base_fee = executed.next_base_fee
        summary.blocks.append({
            "number": block.number,
            "block_hash": block.block_hash,
            "gas_used": block.gas_used,
            "gas_limit": block.gas_limit,
            "base_fee_per_gas": str(block.base_fee_per_gas),
            "next_base_fee": str(executed.next_base_fee),
            "fee_step": executed.fee_step_explanation,
            "valid_txs": sum(1 for r in executed.receipts if r.valid),
            "invalid_txs": [
                {"index": rec.index, "code": rec.error_code, "detail": rec.detail}
                for rec in executed.invalid
            ],
            "burned": str(executed.total_burned),
            "tipped": str(executed.total_tipped),
        })
        log.step("replay_block_accepted", "block applied", number=block.number,
                 next_base_fee=executed.next_base_fee,
                 invalid=len(executed.invalid))

    return summary


def summary_to_dict(s: ReplaySummary) -> dict:
    return {
        "scenario": s.scenario,
        "protocol_version": s.protocol_version,
        "blocks_total": s.blocks_total,
        "blocks_accepted": s.blocks_accepted,
        "blocks_rejected": s.blocks_rejected,
        "valid_txs": s.valid_txs,
        "invalid_txs": s.invalid_txs,
        "total_burned": str(s.total_burned),
        "total_tipped": str(s.total_tipped),
        "head_number": s.head_number,
        "head_next_base_fee": str(s.head_next_base_fee),
        "failures": s.failures,
        "warnings": s.warnings,
        "blocks": s.blocks,
    }
