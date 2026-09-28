"""Replay-oriented JSONL run log.

One JSON object per line, written to ``logs/merge-runs.jsonl`` by default.
Every line carries the run id, monotonic run seq, UTC timestamp, event kind
and enough intermediate state to replay a problem without the database:

* RUN_START      spec, source rows + fingerprint
* SNAPSHOT       target snapshot rows + fingerprint, duplicate groups
* PLAN           the decided action set with per-row reasons
* VALIDATE_ONLY  validate endpoint stopped after PLAN (nothing executed)
* RUN_COMMIT     action counts
* RUN_REJECT     validation failure with category/code/details
* RUN_FAIL       execution failure with category/code/details
* COMMIT_FAULT   injected fault event before rollback

The log is append-only and best-effort: a failure to write the log never
hides the engine result, but a warning line is emitted instead.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

from .contract import MergePlan, as_public_dict
from .errors import MergeError
from .utils import canonical_json

_LOCK = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JsonlRunLogger:
    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)

    # ------------------------------------------------------------- internal
    def _write(self, event: dict[str, Any]) -> None:
        event = {"ts": utc_now(), **event}
        line = json.dumps(event, ensure_ascii=False, default=str)
        with _LOCK:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def _safe(self, event: dict[str, Any]) -> None:
        try:
            self._write(event)
        except OSError as exc:  # logging must not mask engine results
            with _LOCK:
                with open(self.path + ".dropped", "a", encoding="utf-8") as fh:
                    fh.write(
                        canonical_json(
                            {"ts": utc_now(), "event": "LOG_WRITE_FAILED", "error": str(exc)}
                        )
                        + "\n"
                    )

    # ---------------------------------------------------------------- events
    def run_start(
        self,
        *,
        run_id: str,
        seq: int,
        spec: dict[str, Any],
        source_rows: list[dict[str, Any]],
        source_fingerprint: str,
    ) -> None:
        self._safe(
            {
                "event": "RUN_START",
                "run_id": run_id,
                "run_seq": seq,
                "spec": spec,
                "source_rows": source_rows,
                "source_fingerprint": source_fingerprint,
            }
        )

    def snapshot(
        self,
        *,
        run_id: str,
        seq: int,
        target_rows: list[dict[str, Any]],
        target_fingerprint: str,
        duplicate_groups: list[dict[str, Any]],
    ) -> None:
        self._safe(
            {
                "event": "SNAPSHOT",
                "run_id": run_id,
                "run_seq": seq,
                "target_rows": target_rows,
                "target_fingerprint": target_fingerprint,
                "target_duplicate_groups": duplicate_groups,
            }
        )

    def plan(
        self,
        *,
        run_id: str,
        seq: int,
        plan: MergePlan,
        actions_payload: list[dict[str, Any]],
        decisions_payload: list[dict[str, Any]],
    ) -> None:
        self._safe(
            {
                "event": "PLAN",
                "run_id": run_id,
                "run_seq": seq,
                "counts": plan.counts(),
                "actions": actions_payload,
                "decisions": decisions_payload,
            }
        )

    def stage(self, run_id: str, seq: int, event: str, **extra: Any) -> None:
        self._safe({"event": event, "run_id": run_id, "run_seq": seq, **extra})

    def committed(
        self, *, run_id: str, seq: int, counts: dict[str, int]
    ) -> None:
        self._safe(
            {
                "event": "RUN_COMMIT",
                "run_id": run_id,
                "run_seq": seq,
                "committed_actions": counts,
            }
        )

    def rejected(self, *, run_id: str, seq: int, error: MergeError, stage: str) -> None:
        self._safe(
            {
                "event": "RUN_REJECT",
                "run_id": run_id,
                "run_seq": seq,
                "stage": stage,
                "category": error.category.value,
                "code": error.code,
                "message": error.message,
                "details": error.details,
            }
        )

    def failed(self, *, run_id: str, seq: int, error: MergeError, stage: str) -> None:
        self._safe(
            {
                "event": "RUN_FAIL",
                "run_id": run_id,
                "run_seq": seq,
                "stage": stage,
                "category": error.category.value,
                "code": error.code,
                "message": error.message,
                "details": error.details,
            }
        )

    def fault(self, run_id: str, seq: int, name: str, **extra: Any) -> None:
        self._safe(
            {
                "event": "COMMIT_FAULT",
                "run_id": run_id,
                "run_seq": seq,
                "fault": name,
                **extra,
            }
        )


def actions_payload(plan: MergePlan) -> list[dict[str, Any]]:
    return [
        {
            "seq": a.seq,
            "outcome": a.outcome.value,
            "source_index": a.source_index,
            "target_rowid": a.target_rowid,
            "key": list(a.key),
            "new_values": a.new_values,
        }
        for a in plan.actions
    ]


def decisions_payload(plan: MergePlan) -> list[dict[str, Any]]:
    return [
        {
            "source_index": d.source_index,
            "key": list(d.key),
            "matched": d.matched,
            "target_rowid": d.target_rowid,
            "outcome": d.outcome.value,
            "fired_clause": d.fired_clause,
            "reason": d.reason,
            "clause_results": as_public_dict(d.clause_results),
        }
        for d in plan.decisions
    ]
