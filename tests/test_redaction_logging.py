"""Log redaction and request-correlated structured logging."""

from __future__ import annotations

import json
import logging

import pytest

from secretscan.logging_setup import JsonFormatter, configure_logging
from secretscan.redact import Redactor

AWS = "AKIAFAKE000000000001"
GHP = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
BEARER = "Bearer abcdefghijklmnopqrstuvwxyz123456"


def test_redactor_masks_known_shapes():
    r = Redactor()
    text = f"token {AWS} and {GHP} and {BEARER}"
    out = r.redact(text)
    assert AWS not in out
    assert GHP not in out
    assert "abcdefghijklmnopqrstuvwxyz123456" not in out
    assert "AKIA…01" in out
    assert "ghp_…yz" in out
    assert "Bearer" in out  # scheme label preserved, credential masked


def test_redactor_masks_pgp_block():
    r = Redactor()
    block = (
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
        "xsBNBGfakebodyFAKEFAKEFAKEFAKEFAKE\n"
        "-----END PGP PRIVATE KEY BLOCK-----"
    )
    out = r.redact(block)
    assert "xsBNBGfakebody" not in out
    assert "PRIVATE KEY" not in out or "…" in out


class _ListHandler(logging.Handler):
    """Capture handler-formatted output (unlike caplog, which pre-formats)."""

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def test_emitted_log_lines_contain_request_id_and_no_raw_secret():
    logger = configure_logging(logging.INFO)
    handler = _ListHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    try:
        logger.info(
            "processed credential %s for request",
            AWS,
            extra={"request_id": "req-log-1", "actor": "alice", "step": "scan.file"},
        )
    finally:
        logger.removeHandler(handler)
    assert handler.lines, "no log records captured"
    line = handler.lines[0]
    payload = json.loads(line)
    assert payload["request_id"] == "req-log-1"
    assert payload["actor"] == "alice"
    assert payload["step"] == "scan.file"
    assert AWS not in line
    assert "AKIA…01" in line
