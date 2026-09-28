"""Diagnostics and redaction tests."""
from __future__ import annotations

from app.diagnostics import ACCEPT, REJECT, UNDETERMINED, RequestDiagnostics
from app.redaction import redact_text
from app.storage.registry import VersionNotFoundError


def test_redaction_never_contains_text():
    secret = "super-secret-text-123"
    masked = redact_text(secret, reveal=False)
    assert secret not in masked
    assert masked.startswith(f"len={len(secret)},sha256=")
    # Deterministic fingerprint.
    assert redact_text(secret) == masked


def test_redaction_reveal_mode_is_explicit():
    assert "preview" in redact_text("abc", reveal=True)
    assert "preview" not in redact_text("abc", reveal=False)


def test_diagnostics_carries_request_id_and_reasons(registry):
    diag = RequestDiagnostics("req-fixed-id")
    registry.publish([{"surface": "研究", "frequency": 5}])
    registry.segment("研究", diag)

    view = diag.public_view()
    assert view["request_id"] == "req-fixed-id"
    stages = [e["stage"] for e in view["events"]]
    assert stages == ["version_resolution", "normalization", "dag_search"]
    for event in view["events"]:
        assert event["decision"] in {ACCEPT, REJECT, UNDETERMINED}
        assert event["reason"]  # every event explains why
        assert "elapsed_ms" in event

    # State must never echo the raw input; it only carries the fingerprint.
    norm_event = view["events"][1]
    input_desc = norm_event["state"]["input"]
    assert "研究" not in str(input_desc)
    assert input_desc.startswith("len=2,sha256=")


def test_undetermined_gap_is_explained(registry):
    diag = RequestDiagnostics("req-unique")
    registry.publish([{"surface": "a", "frequency": 5}])
    registry.segment("a", diag)
    search = [e for e in diag.public_view()["events"] if e["stage"] == "dag_search"][0]
    # "unique_path" is the documented undetermined condition for the gap.
    assert search["state"]["gap_status"] == "unique_path"
    assert search["state"]["gap"] is None


def test_rejected_version_records_reason(registry):
    diag = RequestDiagnostics("req-badver")
    registry.publish([{"surface": "a", "frequency": 5}])
    try:
        registry.segment("a", diag, version_ref="v-missing")
    except VersionNotFoundError:
        pass
    events = diag.public_view()["events"]
    assert events[-1]["decision"] == REJECT
    assert events[-1]["stage"] == "version_resolution"
    assert events[-1]["state"]["requested"] == "v-missing"


def test_publish_rejection_records_failure_categories(registry):
    from app.storage.models import PublishValidationError

    try:
        registry.prepare([{"surface": ""}, {"surface": "z", "frequency": -1}])
    except PublishValidationError as exc:
        codes = {i["code"] for i in exc.issues}
    else:  # pragma: no cover
        raise AssertionError("expected rejection")
    assert "surface_empty" in codes
    assert "frequency_invalid" in codes
