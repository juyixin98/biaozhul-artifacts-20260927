"""Diagnostics: request-scoped decision records with redaction.

Every API request gets a ``request_id`` and records one :class:`Diagnostic`
explaining *why* it was accepted, rejected, or could not be decided. The
records form an in-memory ring buffer (bounded, no sensitive content ever
stored — inputs are kept only as :func:`redact_text` previews) so the
``/diagnostics`` endpoint can show recent decisions without a log shipping
dependency.

Failure categories are explicit ``reason`` codes, never free-form strings:

EMPTY_TEXT, TEXT_TOO_LONG, VERSION_NOT_FOUND, VERSION_PINNED_INVALID,
INVALID_PAYLOAD, EMPTY_LEXICON, INTERNAL_ERROR.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from .normalizer import redact_text

logger = logging.getLogger("segback")

# Outcome / reason vocabulary.
ACCEPTED = "accepted"
INDETERMINATE = "indeterminate"
REJECTED = "rejected"

REASON_EMPTY_TEXT = "EMPTY_TEXT"
REASON_TEXT_TOO_LONG = "TEXT_TOO_LONG"
REASON_VERSION_NOT_FOUND = "VERSION_NOT_FOUND"
REASON_VERSION_PINNED_INVALID = "VERSION_PINNED_INVALID"
REASON_INVALID_PAYLOAD = "INVALID_PAYLOAD"
REASON_EMPTY_LEXICON = "EMPTY_LEXICON"
REASON_INTERNAL = "INTERNAL_ERROR"


def new_request_id() -> str:
    """A short, URL-safe correlation id (also returned to the client)."""
    return uuid.uuid4().hex[:12]


@dataclass
class Diagnostic:
    request_id: str
    ts: float
    outcome: str  # accepted | indeterminate | rejected
    reason: str
    version_id: int | None
    pinned_version_id: int | None = None
    # Key machine-readable state (never raw content).
    input_chars: int | None = None
    normalized_chars: int | None = None
    best_cost: float | None = None
    second_best_cost: float | None = None
    cost_gap: float | None = None
    gap_class: str | None = None
    token_count: int | None = None
    unknown_chars: int | None = None
    # Redacted preview only; safe to display.
    input_preview: str | None = None
    detail: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class DiagnosticRecorder:
    """Thread-safe bounded ring buffer + stdlib logging bridge."""

    def __init__(self, capacity: int = 200) -> None:
        self._items: deque[Diagnostic] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def record(self, d: Diagnostic) -> Diagnostic:
        with self._lock:
            self._items.append(d)
        level = {
            ACCEPTED: logging.INFO,
            INDETERMINATE: logging.WARNING,
            REJECTED: logging.WARNING,
        }.get(d.outcome, logging.INFO)
        logger.log(
            level,
            "req=%s outcome=%s reason=%s version=%s chars=%s gap=%s%s",
            d.request_id,
            d.outcome,
            d.reason,
            d.version_id,
            d.input_chars,
            d.cost_gap,
            f" detail={d.detail}" if d.detail else "",
        )
        return d

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            items = list(self._items)[-limit:]
        return [asdict(x) for x in reversed(items)]

    def get(self, request_id: str) -> dict | None:
        with self._lock:
            for d in reversed(self._items):
                if d.request_id == request_id:
                    return asdict(d)
        return None


def make_segment_diagnostic(
    *,
    request_id: str,
    raw_text: str,
    result,
    version_id: int,
    pinned: int | None,
) -> Diagnostic:
    unknown_chars = sum(
        s.norm_end - s.norm_start for s in result.segments if s.type == "unknown"
    )
    return Diagnostic(
        request_id=request_id,
        ts=time.time(),
        outcome=result.decision,
        reason="OK",
        version_id=version_id,
        pinned_version_id=pinned,
        input_chars=len(raw_text),
        normalized_chars=len(result.normalized_text),
        best_cost=round(result.best_cost, 6),
        second_best_cost=(
            None if result.second_best_cost is None else round(result.second_best_cost, 6)
        ),
        cost_gap=None if result.cost_gap is None else round(result.cost_gap, 6),
        gap_class=result.gap_class,
        token_count=len(result.segments),
        unknown_chars=unknown_chars,
        input_preview=redact_text(raw_text),
        detail=None if result.decision == ACCEPTED else "best and second-best costs within close gap",
    )


def make_error_diagnostic(
    *,
    request_id: str,
    outcome: str,
    reason: str,
    raw_text: str | None = None,
    version_id: int | None = None,
    pinned: int | None = None,
    input_chars: int | None = None,
    detail: str | None = None,
) -> Diagnostic:
    return Diagnostic(
        request_id=request_id,
        ts=time.time(),
        outcome=outcome,
        reason=reason,
        version_id=version_id,
        pinned_version_id=pinned,
        input_chars=input_chars if input_chars is not None else (None if raw_text is None else len(raw_text)),
        input_preview=None if raw_text is None else redact_text(raw_text),
        detail=detail,
    )
