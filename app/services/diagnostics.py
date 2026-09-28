"""Structured diagnostics.

Every acceptance, rejection and indeterminate outcome produces one JSON line
carrying a request id and the key state behind the decision
(base/head snapshots, overlapping partitions, stages reached).  Sensitive
values are redacted via :func:`app.kernel.errors.redact` before serialisation.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from app.kernel.errors import redact


class Diagnostics:
    """Append-only JSONL diagnostic sink; thread-safe."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(
        self,
        *,
        request_id: str,
        event: str,
        table: str | None = None,
        decision: str = "INFO",
        reason: str | None = None,
        **state: Any,
    ) -> None:
        record = {
            "ts": round(time.time(), 6),
            "request_id": request_id,
            "event": event,
            "table": table,
            "decision": decision,
            "reason": reason,
            "state": redact(state),
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def accepted(self, request_id: str, event: str, **state: Any) -> None:
        self.emit(
            request_id=request_id, event=event, decision="ACCEPTED", **state
        )

    def rejected(self, request_id: str, event: str, reason: str, **state: Any) -> None:
        self.emit(
            request_id=request_id,
            event=event,
            decision="REJECTED",
            reason=reason,
            **state,
        )

    def indeterminate(self, request_id: str, event: str, reason: str, **state: Any) -> None:
        self.emit(
            request_id=request_id,
            event=event,
            decision="INDETERMINATE",
            reason=reason,
            **state,
        )
