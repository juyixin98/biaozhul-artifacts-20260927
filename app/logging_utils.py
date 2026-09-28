"""Leak-safe structured logging.

Two independent guards, belt and suspenders:

1. ``emit`` only serializes an *allow-list* of keys; callers can never smuggle
   raw text into a log record by passing an extra field.
2. Every value is scanned for the request's banned substrings (the fragments
   seen by that request) and redacted. Numeric / structural values are
   rendered through ``repr``; free-form strings are additionally bounded.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any, Iterable

_ALLOWED_KEYS = frozenset(
    {
        "event",
        "request_id",
        "session_id",
        "rule_profile",
        "rule_version",
        "rule_id",
        "priority",
        "start",
        "end",
        "input_len",
        "output_len",
        "matches",
        "uncertain",
        "chunk_index",
        "final",
        "status_code",
        "error_category",
        "detail",
        "db_path",
        "duration_ms",
        "replaced_length",
        "original_length",
        "kind",
        "position",
    }
)

_MAX_VALUE_LEN = 300


class SafeLogger:
    def __init__(self, name: str = "logsafe") -> None:
        self._log = logging.getLogger(name)
        if not self._log.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._log.addHandler(handler)
            self._log.setLevel(logging.INFO)
        # Propagate so pytest's caplog observes records as well; the stderr
        # handler above remains the operational sink.
        self._log.propagate = True

    def emit(
        self,
        level: int,
        event: str,
        banned: Iterable[str] = (),
        **fields: Any,
    ) -> None:
        banned = sorted({b for b in banned if b and len(b) >= 4}, key=len, reverse=True)
        payload: dict[str, Any] = {"event": event}
        for key, value in fields.items():
            if key not in _ALLOWED_KEYS:
                # Free-form key names themselves are structural; keep name,
                # never the unvetted value.
                continue
            payload[key] = self._clean(value, banned)
        try:
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except TypeError:
            line = json.dumps({"event": event, "log_error": "unserializable"})
        self._log.log(level, line)

    def _clean(self, value: Any, banned: list[str]) -> Any:
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return round(value, 3)
        if isinstance(value, (list, tuple)):
            return [self._clean(v, banned) for v in value][:20]
        if isinstance(value, dict):
            return {
                k: self._clean(v, banned)
                for k, v in value.items()
                if k in _ALLOWED_KEYS
            }
        text = str(value)
        for secret in banned:
            if secret in text:
                text = text.replace(secret, "<BANNED>")
        if len(text) > _MAX_VALUE_LEN:
            text = text[:_MAX_VALUE_LEN] + "...<truncated>"
        return text

    def info(self, event: str, **fields: Any) -> None:
        self.emit(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self.emit(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self.emit(logging.ERROR, event, **fields)


safe_log = SafeLogger()
