"""Offline, deterministic replay of synthetic block payloads.

Assembly line tying the layers together::

    JSON/bytes payload  --decode--> transactions (sender recovered by signature)
        --ChainState.apply_block--> validated block + receipts (charge/burn)
        --IndexStore--> durable, indexed rows

There is deliberately no network client, no RPC and no forecasting: inputs are
local synthetic fixtures and the next base fee is derived only from the parent.

Semantics
---------
* A replay reconstructs authoritative state **from genesis** by walking the
  payloads in order, so the result never depends on mutable residual state.
* Persistence is idempotent: a block whose number is already stored is skipped
  after its hash is checked against the stored hash (a mismatch is a failure).
  Re-running therefore never double-counts.
* Blocks are validated strictly in parent order; the first invalid block stops
  the run and is reported with its failure category.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import DEFAULT_GAS_LIMIT
from ..core.state import BlockResult, ChainState
from ..errors import ModelError
from ..storage.store import IndexStore
from .events import EventLog


@dataclass
class BlockPayload:
    number: int
    raw_transactions: list[bytes]
    tx_gas_used: list[int] = field(default_factory=list)


@dataclass
class ReplayReport:
    request_id: str
    applied: list[int]
    skipped: list[int]
    results: dict[int, BlockResult]
    conservation: dict
    failures: list[dict]

    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "applied": self.applied,
            "skipped": self.skipped,
            "blocks": {str(n): self.results[n].summary()
                       for n in sorted(self.results)},
            "conservation": self.conservation,
            "failures": self.failures,
        }


def payload_from_dict(data: dict) -> BlockPayload:
    def _b(v: str) -> bytes:
        return bytes.fromhex(v[2:] if v.startswith("0x") else v)

    return BlockPayload(
        number=int(data["number"]),
        raw_transactions=[_b(t) for t in data.get("raw_transactions", [])],
        tx_gas_used=[int(g) for g in data.get("tx_gas_used", [])],
    )


class Replayer:
    def __init__(self, store: IndexStore, genesis_base_fee: int,
                 gas_limit: int = DEFAULT_GAS_LIMIT,
                 alloc: dict[str, int] | None = None,
                 genesis_gas_used: int | None = None,
                 chain_id: int | None = None,
                 log: EventLog | None = None):
        self.store = store
        self.gas_limit = gas_limit
        self.genesis_base_fee = genesis_base_fee
        self.alloc = alloc or {}
        self.genesis_gas_used = genesis_gas_used
        self.chain_id = chain_id
        self.log = log or EventLog()

    def _fresh_state(self) -> ChainState:
        state = ChainState(genesis_base_fee=self.genesis_base_fee,
                           gas_limit=self.gas_limit,
                           genesis_gas_used=self.genesis_gas_used,
                           chain_id=self.chain_id)
        for addr_hex, balance in self.alloc.items():
            state.add_account(bytes.fromhex(addr_hex.lower().lstrip("0x")),
                              balance=int(balance), nonce=0)
        return state

    def run(self, payloads: list[BlockPayload],
            request_id: str | None = None) -> ReplayReport:
        request_id = request_id or f"replay_{id(self):x}"
        state = self._fresh_state()
        results: dict[int, BlockResult] = {}
        applied: list[int] = []
        skipped: list[int] = []
        failures: list[dict] = []

        self.log.event("replay_start", request_id=request_id,
                       payloads=len(payloads), genesis_base_fee=self.genesis_base_fee)

        for payload in payloads:
            expected_number = state.head.number + 1
            if payload.number != expected_number:
                failures.append({
                    "block": payload.number,
                    "code": "bad_block_number",
                    "message": (f"payload block {payload.number} out of order; "
                                f"expected {expected_number}"),
                })
                self.log.event("replay_failure", request_id=request_id,
                               block=payload.number, code="bad_block_number")
                break

            try:
                block = state.build_block(
                    payload.raw_transactions,
                    tx_gas_used=payload.tx_gas_used or None,
                )
                result = state.apply_block(block)
            except ModelError as exc:
                failures.append({
                    "block": payload.number,
                    "code": exc.code.value,
                    "message": exc.message,
                    "details": exc.details,
                })
                self.log.event("replay_failure", request_id=request_id,
                               block=payload.number, code=exc.code.value,
                               message=exc.message)
                break

            # Idempotent persistence with hash verification.
            stored = self.store.get_block(result.number)
            if stored is not None:
                if stored["hash"] != result.hash.hex():
                    failures.append({
                        "block": result.number,
                        "code": "duplicate_block",
                        "message": ("stored block hash differs from replay "
                                    "result; fixture is not deterministic"),
                        "details": {"stored": stored["hash"],
                                    "replayed": result.hash.hex()},
                    })
                    self.log.event("replay_failure", request_id=request_id,
                                   block=result.number,
                                   code="duplicate_block")
                    break
                skipped.append(result.number)
                self.log.event("replay_skip", request_id=request_id,
                               block=result.number)
            else:
                self.store.save_block_result(state, result)
                applied.append(result.number)
                self.log.event(
                    "replay_apply", request_id=request_id,
                    block=result.number, base_fee=result.base_fee,
                    next_base_fee=result.next_base_fee,
                    gas_used=result.gas_used, burned=result.burned,
                    tips=result.tips, tx_count=len(result.receipts))

            results[result.number] = result

        conservation = state.conservation_report()
        self.log.event("replay_done", request_id=request_id,
                       applied=len(applied), skipped=len(skipped),
                       failures=len(failures), conserved=conservation["conserved"])
        return ReplayReport(
            request_id=request_id, applied=applied, skipped=skipped,
            results=results, conservation=conservation, failures=failures,
        )
