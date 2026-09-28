"""Request-scoped diagnostics.

A :class:`RequestDiagnostics` instance accumulates structured ``events`` for
one request. Every event names a stage, a decision (``accept`` / ``reject`` /
``undetermined``) and the key state that explains *why* the decision was
taken. Payloads are JSON-safe; raw text is never stored, only its redaction
fingerprint.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .redaction import redact_text

ACCEPT = "accept"
REJECT = "reject"
UNDETERMINED = "undetermined"


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass
class Event:
    stage: str
    decision: str
    reason: str
    state: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: Optional[float] = None


class RequestDiagnostics:
    """Collects events for a single request and exposes a JSON-safe view."""

    def __init__(self, request_id: Optional[str] = None, *, reveal_text: bool = False) -> None:
        self.request_id = request_id or new_request_id()
        self._reveal_text = reveal_text
        self.events: list[Event] = []
        self._start = time.perf_counter()

    def _now_ms(self) -> float:
        return round((time.perf_counter() - self._start) * 1000.0, 3)

    def add(self, stage: str, decision: str, reason: str, **state: Any) -> Event:
        event = Event(stage=stage, decision=decision, reason=reason, state=state, elapsed_ms=self._now_ms())
        self.events.append(event)
        return event

    def text_fingerprint(self, text: str) -> str:
        return redact_text(text, reveal=self._reveal_text)

    def public_view(self) -> dict[str, Any]:
        """Diagnostics safe to return to the client and write to logs."""
        return {
            "request_id": self.request_id,
            "events": [
                {
                    "stage": e.stage,
                    "decision": e.decision,
                    "reason": e.reason,
                    "state": e.state,
                    "elapsed_ms": e.elapsed_ms,
                }
                for e in self.events
            ],
        }
