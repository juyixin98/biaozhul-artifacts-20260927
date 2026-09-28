"""Audit logging: request correlation, JSON structure, redaction on disk."""

import json

from conftest import EXPECTED_MASKS, GHP_TOKEN
from secretscan import audit


def test_request_context_sets_identity_and_resets_afterwards():
    assert audit.current_request_id() is None
    with audit.RequestContext.create("rid-x", "actor-y"):
        assert audit.current_request_id() == "rid-x"
        assert audit.current_actor_id() == "actor-y"
    assert audit.current_request_id() is None
    assert audit.current_actor_id() is None


def test_request_context_generates_ids_when_omitted():
    with audit.RequestContext.create() as ctx:
        assert ctx.request_id.startswith("req-")
        assert ctx.actor_id == audit.DEFAULT_ACTOR


def test_json_formatter_includes_identity_fields(tmp_path):
    log_file = tmp_path / "audit.log"
    logger, _ = audit.configure_logging(log_file)
    with audit.RequestContext.create("rid-j", "actor-j"):
        audit.audit_event(
            logger, action="finding.new", target_type="finding",
            target="rule:abcdef", rule_id="github-classic-pat",
            mask="ghp_****xxxx")
    for handler in logger.handlers:
        handler.flush()
    lines = [json.loads(l) for l in log_file.read_text().splitlines()]
    event = next(l for l in lines if l["action"] == "finding.new")
    assert event["request_id"] == "rid-j"
    assert event["actor_id"] == "actor-j"
    assert event["rule_id"] == "github-classic-pat"
    assert event["target"] == "rule:abcdef"
    assert event["outcome"] == "ok"


def test_log_file_never_contains_raw_secret_end_to_end(
        tmp_path, rule_pack, scope_pack, fingerprinter):
    """Full service run with a log file: raw secret must not hit disk."""
    from secretscan import service, state
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text(f'TOKEN = "{GHP_TOKEN}"\n')
    log_file = tmp_path / "audit.log"
    logger, _ = audit.configure_logging(log_file)
    conn = state.connect(tmp_path / "ws.db")
    svc = service.ScanService(conn, rule_pack, scope_pack, fingerprinter,
                              logger)
    svc.run_scan(repo, audit.RequestContext.create("rid-log", "tester"))
    conn.close()
    for handler in logger.handlers:
        handler.flush()
    raw = log_file.read_text()
    assert GHP_TOKEN not in raw
    # The mask does appear, proving the secret was processed and scrubbed.
    assert "ghp_" in raw and "*" in raw
    # Every line is parseable JSON carrying the request id.
    for line in raw.splitlines():
        event = json.loads(line)
        assert event["request_id"] == "rid-log"
        assert "actor_id" in event and "action" in event


def test_redactor_handles_accidental_secret_in_format_args(tmp_path):
    log_file = tmp_path / "audit.log"
    logger, redactor = audit.configure_logging(log_file)
    redactor.register([GHP_TOKEN])
    # Simulate a buggy log statement that includes the raw value via %s.
    logger.error("debug dump: token=%s", GHP_TOKEN)
    for handler in logger.handlers:
        handler.flush()
    content = log_file.read_text()
    assert GHP_TOKEN not in content
    assert EXPECTED_MASKS["ghp"] in content


def test_log_lines_separate_outcome_field_for_failures(tmp_path):
    log_file = tmp_path / "audit.log"
    logger, _ = audit.configure_logging(log_file)
    audit.audit_event(logger, action="api.denied", target_type="root",
                      target="/elsewhere", outcome="denied",
                      reason_code="root_not_allowed")
    for handler in logger.handlers:
        handler.flush()
    event = json.loads(log_file.read_text().strip())
    assert event["outcome"] == "denied"
    assert event["reason_code"] == "root_not_allowed"
