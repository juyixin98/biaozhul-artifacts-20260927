"""Diagnostics tests: request correlation, state content, secret redaction."""

import json
import logging

from merge3.diagnostics import (
    DiagnosticLogger,
    digest,
    document_state,
    new_request_id,
    redact_sensitive,
    scrub_text,
)


def test_request_ids_are_unique_and_correlated():
    d1 = DiagnosticLogger()
    d2 = DiagnosticLogger()
    assert d1.request_id != d2.request_id
    assert d1.request_id.startswith("req_")
    rec = d1.event("ping", {"k": 1}, "ok")
    assert rec["request_id"] == d1.request_id
    # all records on one logger carry the same correlation id
    rec2 = d1.event("pong")
    assert rec2["request_id"] == d1.request_id


def test_document_state_never_contains_content():
    secret = "topsecret-line\n" * 3
    state = document_state("base", secret)
    serialized = json.dumps(state)
    assert "topsecret" not in serialized
    assert state["chars"] == len(secret)
    assert state["sha256_12"] == digest(secret)
    assert state["eol_counts"]["lf"] == 3


def test_sensitive_fields_are_redacted_with_metadata():
    payload = {
        "api_key": "sk-live-abcdef1234567890",
        "nested": {"password": "hunter2", "ok": "fine"},
        "list": [{"token": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}],
    }
    out = redact_sensitive(payload)
    blob = json.dumps(out)
    assert "sk-live-abcdef1234567890" not in blob
    assert "hunter2" not in blob
    assert "aaaa" not in blob
    assert "<redacted:len=" in blob
    assert out["nested"]["ok"] == "fine"  # non-sensitive data survives


def test_free_text_scrub_catches_credential_patterns():
    text = "call failed token=abcdef1234567890ZZZ and Bearer QQQQQQQQQQ"
    scrubbed = scrub_text(text)
    assert "abcdef1234567890ZZZ" not in scrubbed
    assert "QQQQQQQQQQ" not in scrubbed
    assert "<redacted>" in scrubbed


def test_event_levels_and_records_persist():
    d = DiagnosticLogger(redact=True)
    d.event("accepted", {"n": 1}, "all disjoint", level=logging.INFO)
    d.event("indeterminate", {"n": 2}, "need choice", level=logging.WARNING)
    assert [r["event"] for r in d.records] == [
        "accepted", "indeterminate"]
    assert d.records[1]["reason"] == "need choice"


def test_redaction_can_be_disabled_only_explicitly():
    payload = {"password": "hunter2"}
    out = redact_sensitive(payload)  # default: redact
    assert "hunter2" not in json.dumps(out)
