"""Structured, request-correlated event log.

Every log line is a single JSON object written to stderr so it is
machine-greppable and never corrupts API stdout. Each event carries:

    ts          UTC timestamp
    request_id  correlation id (echoed back to the caller)
    component   where it was produced (encoding / core / storage / replay / api)
    version     model version
    event       step name
    level       info | warning | error
    ...fields   step-specific key/values

Failures are logged at ``error`` level and anything uncertain is a
``warning`` with an explicit ``uncertainty`` field so the two categories stay
separate from the happy path in both logs and API responses.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from .. import __version__


class EventLog:
    def __init__(self, stream=None, enabled: bool = True):
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled
        self.records: list[dict] = []

    def _emit(self, level: str, event: str, component: str,
              request_id: str | None, **fields) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "event": event,
            "component": component,
            "version": __version__,
            "request_id": request_id,
        }
        record.update(fields)
        self.records.append(record)
        if self.enabled:
            try:
                self.stream.write(json.dumps(record, sort_keys=True) + "\n")
                self.stream.flush()
            except (ValueError, BrokenPipeError):  # pragma: no cover
                pass

    def event(self, event: str, component: str = "core",
              request_id: str | None = None, **fields) -> None:
        self._emit("info", event, component, request_id, **fields)

    def info(self, event: str, component: str = "core",
             request_id: str | None = None, **fields) -> None:
        self._emit("info", event, component, request_id, **fields)

    def warning(self, event: str, uncertainty: str, component: str = "core",
                request_id: str | None = None, **fields) -> None:
        self._emit("warning", event, component, request_id,
                   uncertainty=uncertainty, **fields)

    def error(self, event: str, code: str, message: str,
              component: str = "core", request_id: str | None = None,
              **fields) -> None:
        self._emit("error", event, component, request_id,
                   code=code, message=message, **fields)

    def records_for(self, request_id: str) -> list[dict]:
        return [r for r in self.records if r.get("request_id") == request_id]
