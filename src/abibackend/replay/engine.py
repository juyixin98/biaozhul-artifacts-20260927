"""Offline replay engine.

Replays the deterministic scenario (or caller-supplied transactions) against a
fresh in-memory chain, records every step with its correlation/run id and the
precise verdict, persists to the indexed store, and returns a report. Failures
are first-class: they are recorded with an error category and never counted or
reported as success.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from ..chain import ChainError, ChainState, Receipt, Transaction, apply_transaction
from ..config import SETTINGS
from ..log_utils import get_logger, run_context
from ..storage import Repository
from .fixtures import Actors, bootstrap, build_scenario, make_actors

log = get_logger("replay")


@dataclass
class StepRecord:
    index: int
    sender: str
    nonce: int
    call: str
    ok: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ReplayReport:
    run_id: str
    mode: str
    started_at: str
    finished_at: str
    total: int
    succeeded: int
    failed: int
    final_state_root: str
    steps: List[StepRecord]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def replay(
    repo: Repository,
    mode: str = "scenario",
    transactions: Optional[List[Transaction]] = None,
    run_id: Optional[str] = None,
    chain_id: Optional[int] = None,
    seed: Optional[int] = None,
) -> ReplayReport:
    run_id = run_id or f"run-{uuid.uuid4()}"
    chain_id = SETTINGS.chain_id if chain_id is None else chain_id
    seed = SETTINGS.replay_seed if seed is None else seed
    started = _utcnow()
    repo.start_run(run_id, started, mode)

    actors: Actors = make_actors(seed)
    state: ChainState = bootstrap(chain_id, actors)
    if transactions is None:
        transactions = build_scenario(chain_id, actors)

    steps: List[StepRecord] = []
    records: List[Dict[str, Any]] = []
    ok_count = fail_count = 0

    with run_context(run_id):
        for i, tx in enumerate(transactions):
            step = _apply_one(i, tx, state)
            steps.append(step)
            if step.ok:
                ok_count += 1
            else:
                fail_count += 1
            records.append(_to_record(tx, step))

        block_number = repo.next_block_number()
        root = state.state_root()
        repo.save_block(block_number, run_id, root, records)
        repo.save_accounts(state)

        finished = _utcnow()
        status = "completed"
        detail = f"{ok_count} ok / {fail_count} failed"
        repo.finish_run(run_id, finished, len(transactions), ok_count, fail_count, status, detail)
        log.info(
            "replay complete",
            extra={"step": "finalize", "verdict": status, "detail": detail},
        )

    return ReplayReport(
        run_id=run_id,
        mode=mode,
        started_at=started,
        finished_at=finished,
        total=len(transactions),
        succeeded=ok_count,
        failed=fail_count,
        final_state_root=root.hex(),
        steps=steps,
    )


def _apply_one(index: int, tx: Transaction, state: ChainState) -> StepRecord:
    sender_hex = hex(tx.sender)
    try:
        receipt: Receipt = apply_transaction(state, tx)
    except ChainError as exc:
        log.warning(
            "transaction rejected",
            extra={
                "step": f"apply[{index}]",
                "verdict": "fail",
                "type": type(exc).error_code,
                "reason": str(exc),
                "offset": None,
            },
        )
        return StepRecord(
            index=index, sender=sender_hex, nonce=tx.nonce, call="",
            ok=False, error_code=type(exc).error_code, error_message=str(exc),
        )
    except Exception as exc:  # never mask an unexpected error as success
        log.error(
            "unexpected error during apply",
            extra={"step": f"apply[{index}]", "verdict": "error", "reason": repr(exc)},
        )
        return StepRecord(
            index=index, sender=sender_hex, nonce=tx.nonce, call="",
            ok=False, error_code="unexpected_error", error_message=repr(exc),
        )

    events = [{"name": e.name, "args": _jsonable(e.args)} for e in receipt.events]
    log.info(
        "transaction applied",
        extra={
            "step": f"apply[{index}]",
            "verdict": "ok",
            "type": receipt.call,
            "detail": f"events={len(events)} root={receipt.state_root.hex()[:12]}",
        },
    )
    return StepRecord(
        index=index, sender=sender_hex, nonce=tx.nonce, call=receipt.call,
        ok=True, events=events,
    )


def _jsonable(args: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in args.items():
        out[k] = hex(v) if isinstance(v, int) else v
    return out


def _to_record(tx: Transaction, step: StepRecord) -> Dict[str, Any]:
    return {
        "transaction": tx,
        "ok": step.ok,
        "call": step.call,
        "error_code": step.error_code,
        "error_message": step.error_message,
        "events": [
            type("E", (), {"name": e["name"], "args": e["args"]})() for e in step.events
        ],
    }
