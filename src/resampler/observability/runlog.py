"""Structured per-run logger for replayable diagnostics.

Every API request and every test gets a run id and a directory

    <log_dir>/runs/<run_id>/
        events.jsonl     append-only event stream
        summary.json     final outcome + key state

Tests additionally store signal artifacts (input/output/reference .npy)
plus an ``assertions.log`` stating each concrete result and the reason it
passed or failed.  Nothing here swallows exceptions: logging failures must
never mask a computation failure.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any


class RunLogger:
    def __init__(self, log_dir: str, run_id: str | None = None):
        self.log_dir = log_dir
        self.run_id = run_id or time.strftime("run-%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8]
        self.dir = os.path.join(log_dir, "runs", self.run_id)
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()
        self._path = os.path.join(self.dir, "events.jsonl")
        self._assertions: list[dict[str, Any]] = []
        self.event("run_started", {"pid": os.getpid()})

    def _write(self, rec: dict[str, Any]) -> None:
        line = json.dumps(rec, sort_keys=True, default=str, ensure_ascii=False)
        with self._lock, open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def event(self, name: str, data: dict[str, Any] | None = None) -> None:
        rec = {"ts": round(time.time(), 6), "run_id": self.run_id,
               "event": name, "data": data or {}}
        self._write(rec)

    def state(self, component: str, state: dict[str, Any]) -> None:
        self.event("state", {"component": component, "state": state})

    def assertion(self, name: str, passed: bool, detail: dict[str, Any],
                  reason: str) -> None:
        rec = {"name": name, "passed": bool(passed), "reason": reason,
               "detail": detail}
        with self._lock:
            self._assertions.append(rec)
        self._write({"ts": round(time.time(), 6), "run_id": self.run_id,
                     "event": "assertion", "data": rec})

    def artifact_text(self, filename: str, text: str) -> str:
        path = os.path.join(self.dir, filename)
        with self._lock, open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def artifact_json(self, filename: str, obj: Any) -> str:
        return self.artifact_text(filename, json.dumps(obj, indent=2, default=str))

    def summary(self, outcome: str, extra: dict[str, Any] | None = None) -> str:
        passed = [a for a in self._assertions if a["passed"]]
        failed = [a for a in self._assertions if not a["passed"]]
        data = {"run_id": self.run_id, "outcome": outcome,
                "assertions_passed": len(passed),
                "assertions_failed": len(failed),
                "failed_names": [a["name"] for a in failed]}
        if extra:
            data.update(extra)
        with self._lock, open(os.path.join(self.dir, "summary.json"), "w",
                              encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        self.event("run_finished", {"outcome": outcome})
        return self.dir


def new_run_logger(log_dir: str, run_id: str | None = None) -> RunLogger:
    return RunLogger(log_dir=log_dir, run_id=run_id)
