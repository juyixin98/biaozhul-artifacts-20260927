"""Service orchestration tests: request ids, audit-on-reject, diagnostics shape."""

from __future__ import annotations


def test_service_returns_request_id_and_persists_audit(service):
    resp = service.review(
        template="SELECT id FROM users WHERE id = ?", params={"0": 1})
    assert resp.request_id.startswith("req_")
    record = service.fetch_audit(resp.request_id)
    assert record is not None
    assert record["verdict"] == "accept"
    assert record["statement_type"] == "SELECT"


def test_service_audits_rejections_too(service):
    resp = service.review(
        template="SELECT id FROM ?", params={"0": "users"})
    assert resp.result["verdict"] == "reject"
    record = service.fetch_audit(resp.request_id)
    assert record["verdict"] == "reject"
    assert "VALUE_PARAM_AS_IDENTIFIER" in record["finding_codes"]


def test_client_request_id_is_used(service):
    resp = service.review(template="SELECT 1", request_id="trace-123")
    assert resp.request_id == "trace-123"
    assert service.fetch_audit("trace-123") is not None


def test_each_review_has_unique_request_id(service):
    ids = {service.review(template="SELECT 1").request_id for _ in range(5)}
    assert len(ids) == 5


def test_service_evidence_is_redacted_and_chain_ok(service):
    secret = "very-sensitive-string-value"
    resp = service.review(
        template="SELECT id FROM users WHERE name = ?", params={"0": secret})
    record = service.fetch_audit(resp.request_id)
    import json
    blob = json.dumps(record)
    assert secret not in blob
    diag = record["evidence"]["param_diagnostics"][0]
    assert diag["binding"]["type"] == "str"
    assert diag["binding"]["length"] == len(secret)
    assert service.chain_report()["ok"] is True


def test_service_diagnostics_explain_decision(service):
    resp = service.review(
        template="SELECT id FROM users WHERE id IN (?)", params={"0": []})
    diags = resp.result["param_diagnostics"]
    assert diags[0]["decision"] == "rejected"
    assert "empty array" in diags[0]["reason"]
    assert diags[0]["in_expansion_list"] is True


def test_service_diagnostics_identify_context_and_key(service):
    resp = service.review(
        template="UPDATE users SET name = ? WHERE id = ?",
        params={"0": "x", "1": 1})
    contexts = {(d["key"], d["context"]) for d in resp.result["param_diagnostics"]}
    assert ("0", "set_rhs") in contexts
    assert ("1", "where") in contexts
