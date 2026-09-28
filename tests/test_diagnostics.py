"""Tests for diagnostics: request ids and sensitive-data redaction."""
from __future__ import annotations

import json
import logging

from smt.observability import (
    configure_logging,
    event,
    new_request_id,
    redact_key,
    redact_value,
)


def test_request_id_shape():
    rid = new_request_id()
    assert len(rid) == 32 and all(c in "0123456789abcdef" for c in rid)
    assert new_request_id() != rid


def test_redact_key_keeps_only_prefix():
    full = "ab" * 32
    red = redact_key(full)
    assert red.startswith("abababab")
    assert "…" in red
    assert full not in red  # the full key must never appear
    assert redact_key(None) is None


def test_redact_value_hides_content():
    assert redact_value(None) == {"present": False}
    secret = b"super-secret-password".hex()
    red = redact_value(secret)
    assert red == {"present": True, "byte_length": 21}
    assert "secret" not in json.dumps(red)
    assert "password" not in json.dumps(red)


def test_json_log_is_machine_readable_and_redacted(caplog):
    logger = configure_logging(level="DEBUG", fmt="json")
    # attach pytest's handler to capture; also ensure formatter output itself
    import io
    import logging as _logging
    stream = io.StringIO()
    h = _logging.StreamHandler(stream)
    from smt.observability.diagnostics import JsonFormatter
    h.setFormatter(JsonFormatter())
    logger.addHandler(h)
    try:
        with caplog.at_level(logging.INFO, logger="smt"):
            event(
                logger, logging.WARNING, "proof verification", "req-1",
                verdict="root_mismatch",
                key=redact_key("cd" * 32),
                value=redact_value(b"secret-bytes".hex()),
                root=("ab" * 32)[:16],
            )
    finally:
        logger.removeHandler(h)

    line = stream.getvalue().strip().splitlines()[-1]
    record = json.loads(line)  # must be valid JSON
    assert record["message"] == "proof verification"
    assert record["context"]["request_id"] == "req-1"
    assert record["context"]["verdict"] == "root_mismatch"
    assert record["context"]["key"].startswith("cdcdcdcd") and "…" in record["context"]["key"]
    assert record["context"]["value"] == {"present": True, "byte_length": 12}
    assert "secret-bytes" not in line
