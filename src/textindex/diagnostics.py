"""Run identity and structured replay logging.

Every operation that can fail is recorded as one JSON object per line with:

* ``run_id``      — service-process id (UTC timestamp + short random suffix)
* ``seq``         — monotonically increasing per process, starting at 1
* ``ts``          — ISO-8601 UTC timestamp
* ``op``          — e.g. ``documents/create``, ``edits/apply``,
                    ``positions/convert``
* ``outcome``     — ``ok`` | ``input_error`` | ``state_conflict`` |
                    ``resource_exhausted`` | ``computation_failure``
* ``code``        — stable error code on failure
* ``key``         — logical target (doc id, or "-")
* ``intermediate``— small bounded snapshots (positions, window, decisions),
                    enough to replay the judgement without a debugger
* ``reason``      — human-readable justification on failure
* ``request_id``  — echoed HTTP request id when present

The service log (all ops) and the test log (test-coded runs, including
expected failures and independent oracle verdicts) are separate files, so
replay tooling can replay the test log end-to-end without a live server.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import TextIndexError


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + \
        secrets.token_hex(3)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class RunLogger:
    """Thread-safe JSONL appender."""

    path: str
    run_id: str = field(default_factory=new_run_id)
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _fh: Any = None

    def __post_init__(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        # line-buffered text mode; each record is a complete line.
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)

    def record(
        self,
        *,
        op: str,
        outcome: str,
        key: str = "-",
        code: str | None = None,
        intermediate: dict[str, Any] | None = None,
        reason: str | None = None,
        request_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            entry = {
                "run_id": self.run_id,
                "seq": self._seq,
                "ts": _now(),
                "op": op,
                "outcome": outcome,
                "key": key,
            }
            if code is not None:
                entry["code"] = code
            if intermediate:
                entry["intermediate"] = _bounded(intermediate)
            if reason is not None:
                entry["reason"] = reason
            if request_id is not None:
                entry["request_id"] = request_id
            if extra:
                entry["extra"] = extra
            self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def record_error(
        self,
        *,
        op: str,
        exc: TextIndexError,
        key: str = "-",
        intermediate: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.record(
            op=op,
            outcome=exc.category,
            key=key,
            code=exc.code,
            intermediate=intermediate,
            reason=exc.message,
            request_id=request_id,
            extra={"details": exc.details},
        )

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _bounded(values: dict[str, Any], *, max_text: int = 80) -> dict[str, Any]:
    """Trim large payloads in intermediate snapshots.

    Keys prefixed ``replay_`` are kept whole: they exist specifically so a
    replayer can fully rebuild the case, so truncating them would defeat it.
    """
    out: dict[str, Any] = {}
    for k, v in values.items():
        if k.startswith("replay_"):
            out[k] = v
        elif isinstance(v, str) and len(v) > max_text:
            out[k] = v[:max_text] + f"…<+{len(v) - max_text} chars>"
        elif isinstance(v, (list, tuple)) and len(v) > 32:
            out[k] = list(v[:32]) + [f"…<+{len(v) - 32} more>"]
        else:
            out[k] = v
    return out
