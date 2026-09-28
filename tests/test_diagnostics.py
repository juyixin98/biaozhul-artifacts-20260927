"""Diagnostics and redaction tests.

Two complementary guarantees:
- unit level: sensitive scalar values are replaced by stable redaction tokens;
- end-to-end level (test_api.py): raw sensitive values never reach the log.
"""
from __future__ import annotations

import json

from app.kernel.errors import redact
from app.services.diagnostics import Diagnostics


def test_redact_replaces_sensitive_scalars_but_keeps_structure():
    payload = {
        "table": "events",
        "owner": "alice@example.test",
        "nested": {"email": "bob@example.test", "amount": 42},
        "items": [{"token": "s3cr3t", "region": "us"}],
    }
    safe = redact(payload)
    flat = json.dumps(safe)
    assert "alice@example.test" not in flat
    assert "bob@example.test" not in flat
    assert "s3cr3t" not in flat
    # Non-sensitive structure/values survive.
    assert safe["table"] == "events"
    assert safe["nested"]["amount"] == 42
    assert safe["items"][0]["region"] == "us"
    # Redacted tokens are stable hashes (correlatable without disclosure).
    assert safe["owner"].startswith("<redacted:")
    assert redact({"owner": "alice@example.test"})["owner"] == safe["owner"]


def test_diagnostics_lines_carry_request_id_decision_and_state(tmp_path):
    diag = Diagnostics(tmp_path / "diag.jsonl")
    diag.accepted("req-xyz", "commit.committed", table="events",
                  head_snapshot_id=4, rebased=True)
    diag.rejected("req-xyz", "commit.rejected", "partition overlap",
                  table="events", overlapping=["region=us/day=2024-01-01"])
    lines = [json.loads(l) for l in (tmp_path / "diag.jsonl").read_text().splitlines()]
    assert [l["request_id"] for l in lines] == ["req-xyz", "req-xyz"]
    assert [l["decision"] for l in lines] == ["ACCEPTED", "REJECTED"]
    assert lines[0]["state"]["head_snapshot_id"] == 4
    assert lines[1]["state"]["overlapping"] == ["region=us/day=2024-01-01"]
    assert lines[1]["reason"] == "partition overlap"
