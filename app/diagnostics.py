"""Diagnostics: structured, redaction-safe decision records.

Each request gets a ``request_id`` (client supplied ``X-Request-ID`` echoed
back when valid, else server generated). Two kinds of rows are persisted:

* ``request``  — one per inbound HTTP request, with key runtime state;
* ``decision`` — why something was accepted / rejected / is inconclusive.

Redaction contract: pattern and payload *contents* are sensitive and are
NEVER stored. We keep lengths, counts, offsets and automaton state only.
Patterns appear at most as ``"pat#3 (5 bytes)"``. This module owns that rule;
tests assert that raw bytes cannot leak into stored events.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from .storage.diag_repo import DiagnosticRepo

logger = logging.getLogger("ac.diagnostics")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_MAX_SUMMARY = 500


def is_safe_request_id(rid: Optional[str]) -> bool:
    return bool(rid) and bool(_REQUEST_ID_RE.match(rid or ""))


def new_request_id() -> str:
    return uuid.uuid4().hex


def redact_bytes(buf: bytes, *, max_preview: int = 0) -> str:
    """Return a descriptor that cannot reveal contents.

    ``max_preview=0`` (default): length only. Unit tests cover binary content
    with this mode. A small hex preview exists purely for local debugging and
    is not used for stored diagnostic rows.
    """
    if max_preview > 0:
        preview = buf[:max_preview].hex()
        more = "…" if len(buf) > max_preview else ""
        return f"<{len(buf)} bytes: {preview}{more}>"
    return f"<{len(buf)} bytes>"


def pattern_ref(pattern_id: int, length: int) -> str:
    return f"pat#{pattern_id} ({length} bytes)"


def safe_json(obj: Any, *, limit: int = 4000) -> str:
    s = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return s if len(s) <= limit else s[: limit - 15] + "…<truncated>"


class Recorder:
    """One Recorder per request, attached to the request scope."""

    def __init__(self, repo: DiagnosticRepo, request_id: str):
        self._repo = repo
        self.request_id = request_id
        self._events: List[Dict[str, Any]] = []

    # ---- emission -----------------------------------------------------------

    def record_request(
        self,
        *,
        method: str,
        path: str,
        state: Optional[Dict[str, Any]] = None,
    ) -> None:
        summary = f"{method} {path} accepted-for-processing"
        self._emit(
            kind="request",
            method=method,
            path=path,
            decision="accept",
            code=None,
            summary=summary[:_MAX_SUMMARY],
            state=state,
        )

    def record_decision(
        self,
        *,
        decision: str,
        code: Optional[str],
        summary: str,
        method: Optional[str] = None,
        path: Optional[str] = None,
        state: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._emit(
            kind="decision",
            method=method,
            path=path,
            decision=decision,
            code=code,
            summary=summary[:_MAX_SUMMARY],
            state=state,
        )

    def _emit(
        self,
        *,
        kind: str,
        method: Optional[str],
        path: Optional[str],
        decision: Optional[str],
        code: Optional[str],
        summary: str,
        state: Optional[Dict[str, Any]],
    ) -> None:
        state_json = safe_json(state) if state else None
        event_id = self._repo.insert(
            request_id=self.request_id,
            kind=kind,
            method=method,
            path=path,
            decision=decision,
            code=code,
            summary=summary,
            state_json=state_json,
        )
        event = {"id": event_id, "kind": kind, "decision": decision,
                 "code": code, "summary": summary}
        self._events.append(event)
        # Also go to the application log with the request id, never raw data.
        logger.info("req=%s %s code=%s :: %s", self.request_id, kind,
                    code or "-", summary)

    @property
    def events(self) -> List[Dict[str, Any]]:
        return list(self._events)
