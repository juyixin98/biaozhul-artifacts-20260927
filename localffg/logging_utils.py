"""Structured JSON logging tied to a run identity.

Every log line is one JSON object carrying at least:

    ts, level, run_id, component, event

plus event-specific fields. Run ids make logs correlatable to a particular
submission sequence (and to journal rows in SQLite); steps include versions,
progress counters and the classification basis. Nothing here maps an
exception or an unknown state to a success event.
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime, timezone

from . import PROTOCOL_VERSION, __version__


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


class JsonRunLogger:
    def __init__(self, *, run_id: str | None = None, stream=None, level: str = "INFO", echo: bool = True):
        self.run_id = run_id or new_run_id()
        self._stream = stream if stream is not None else sys.stderr
        self._level = getattr(logging, level.upper(), logging.INFO)
        self.echo = echo
        self.records: list[dict] = []

    def _emit(self, level_name: str, event: str, **fields) -> dict:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "level": level_name,
            "run_id": self.run_id,
            "app_version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "event": event,
            **fields,
        }
        self.records.append(rec)
        if self.echo and self._stream is not None:
            self._stream.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
            self._stream.flush()
        return rec

    def info(self, event: str, **fields) -> None:
        if self._level <= logging.INFO:
            self._emit("INFO", event, **fields)

    def warn(self, event: str, **fields) -> None:
        if self._level <= logging.WARNING:
            self._emit("WARN", event, **fields)

    def error(self, event: str, **fields) -> None:
        if self._level <= logging.ERROR:
            self._emit("ERROR", event, **fields)

    def step(self, index: int, total: int, event: str, **fields) -> None:
        """Progress + decision-basis line."""
        self.info(event, progress={"step": index, "of": total}, **fields)
