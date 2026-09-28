"""The service's own logs must never contain original secrets."""
from __future__ import annotations

import json
import logging

import pytest

from app.logging_utils import SafeLogger
from tests.synth_fixtures import SYNTH_API_TOKEN, SYNTH_EMAIL


def test_safe_logger_scrubs_banned_substrings(caplog):
    caplog.set_level(logging.INFO, logger="logsafe-test")
    log = SafeLogger("logsafe-test")
    log.info(
        "redact.complete",
        banned=[SYNTH_EMAIL, SYNTH_API_TOKEN],
        request_id="req_x",
        input_length=123,
        detail=f"processed {SYNTH_EMAIL} and token {SYNTH_API_TOKEN}",
    )
    records = [r.getMessage() for r in caplog.records]
    assert records, "expected a log record"
    line = records[-1]
    payload = json.loads(line)
    assert SYNTH_EMAIL not in line
    assert SYNTH_API_TOKEN not in line
    assert payload["event"] == "redact.complete"
    assert payload["request_id"] == "req_x"  # allowlisted key
    assert "<BANNED>" in payload["detail"]


def test_safe_logger_drops_non_allowlisted_keys(caplog):
    caplog.set_level(logging.INFO, logger="logsafe-test2")
    log = SafeLogger("logsafe-test2")
    log.info("x", raw_text=SYNTH_API_TOKEN, request_id="r")
    line = caplog.records[-1].getMessage()
    assert SYNTH_API_TOKEN not in line
    payload = json.loads(line)
    assert "raw_text" not in payload


def test_api_log_lines_contain_no_secret(client, caplog):
    caplog.set_level(logging.INFO, logger="logsafe")
    client.post(
        "/api/v1/redact",
        json={"text": f"user mail {SYNTH_EMAIL} token {SYNTH_API_TOKEN}"},
    )
    all_text = "\n".join(r.getMessage() for r in caplog.records)
    assert SYNTH_EMAIL not in all_text
    assert SYNTH_API_TOKEN not in all_text
    # Structural evidence that processing happened and was explained.
    events = [json.loads(r.getMessage())["event"] for r in caplog.records]
    assert "redact.complete" in events


def test_audit_denial_is_logged_without_secret(client, caplog):
    caplog.set_level(logging.WARNING, logger="logsafe")
    client.get("/api/v1/audit/requests")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "audit_access_denied" in text
