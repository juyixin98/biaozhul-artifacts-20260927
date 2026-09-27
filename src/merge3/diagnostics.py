"""Structured diagnostics with request correlation and secret redaction.

Every log line carries:

* ``request_id`` — caller-supplied or generated, so a multi-request server
  log can be filtered to one merge;
* ``event`` — what happened (``merge_accepted``, ``merge_conflict``,
  ``merge_rejected``, ``merge_indeterminate``, ``resolution_rebuilt``);
* ``state`` — the key state behind the decision (spans, edit ids, conflict
  type, byte counts, content digests — never the content itself);
* ``reason`` — a plain-English explanation of why the core accepted,
  rejected, or could not decide.

Sensitive data handling: callers may pass an explicit
``sensitive_fields`` mapping; those values are replaced with
``"<redacted:len=N,sha256_12=...>"``.  Document text is *never* logged —
only lengths and short digests — so even a fixture containing a token does
not leak into logs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any, Optional

LOGGER_NAME = "merge3"

#: Patterns that look like credentials, used as a defense-in-depth scrub on
#: free-text detail strings (not on document contents, which are never logged).
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|authorization)"
               r"(\s*[:=]\s*)(\S+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]+)"),
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9]{8,}\b"),
    re.compile(r"\b[0-9a-fA-F]{40,}\b"),  # long hex: token-like
)


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:16]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def scrub_text(value: str) -> str:
    """Replace credential-looking substrings in a free-text *value*."""
    def repl(m: re.Match[str]) -> str:
        if m.lastindex and m.lastindex >= 3:
            return f"{m.group(1)}{m.group(2)}<redacted>"
        if m.lastindex == 2:
            return f"{m.group(1)}<redacted>"
        return "<redacted>"

    out = value
    for pat in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def redact_sensitive(payload: Any, sensitive_fields: Optional[set[str]] = None) -> Any:
    """Deep-copy *payload*, masking keys named in *sensitive_fields*.

    Also masks a small built-in denylist regardless of the caller's list.
    """
    sensitive = set(sensitive_fields or set()) | {
        "password", "passwd", "secret", "token", "api_key", "apikey",
        "authorization", "auth", "credential",
    }

    def walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                if isinstance(k, str) and k.lower() in sensitive:
                    if isinstance(v, str):
                        out[k] = f"<redacted:len={len(v)},sha256_12={digest(v)}>"
                    else:
                        out[k] = "<redacted>"
                else:
                    out[k] = walk(v)
            return out
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        if isinstance(obj, str):
            return scrub_text(obj)
        return obj

    return walk(payload)


def document_state(name: str, text: str) -> dict[str, Any]:
    """Safe summary of a document for logging: metadata only, no content."""
    crlf = text.count("\r\n")
    lone_lf = text.count("\n") - crlf
    lone_cr = len(re.findall(r"\r(?!\n)", text))
    return {
        "document": name,
        "chars": len(text),
        "sha256_12": digest(text),
        "ends_with_newline": text.endswith("\n") or text.endswith("\r"),
        "eol_counts": {"crlf": crlf, "lf": lone_lf, "cr": lone_cr},
        "lines": (text.count("\n") + text.count("\r") - crlf) + (
            1 if text and not text.endswith(("\n", "\r")) else 0),
    }


class DiagnosticLogger:
    """Thin structured logger bound to one request id."""

    def __init__(self, request_id: Optional[str] = None,
                 logger: Optional[logging.Logger] = None,
                 redact: bool = True) -> None:
        self.request_id = request_id or new_request_id()
        self.logger = logger or logging.getLogger(LOGGER_NAME)
        self.redact = redact
        self.records: list[dict[str, Any]] = []

    def event(self, event: str, state: Optional[dict[str, Any]] = None,
              reason: str = "", level: int = logging.INFO,
              sensitive_fields: Optional[set[str]] = None) -> dict[str, Any]:
        record: dict[str, Any] = {
            "request_id": self.request_id,
            "event": event,
            "state": state or {},
            "reason": scrub_text(reason) if self.redact else reason,
        }
        if self.redact:
            record = redact_sensitive(record, sensitive_fields)
        self.records.append(record)
        self.logger.log(level, json.dumps(record, ensure_ascii=False, sort_keys=True))
        return record
