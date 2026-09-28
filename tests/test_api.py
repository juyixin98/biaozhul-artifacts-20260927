"""End-to-end HTTP tests over the FastAPI app (in-process, isolated SQLite)."""
from __future__ import annotations

import copy

from tests.fixtures.fixtures import records as fixture_records, schema as fixture_schema

RUN_HEADER = {"X-Run-ID": "run::http-acceptance"}


def _create(client):
    resp = client.post("/api/v1/batches", json={"schema": fixture_schema(),
                                                "records": fixture_records()},
                       headers=RUN_HEADER)
    assert resp.status_code == 201 or resp.status_code == 200, resp.text
    return resp.json()


def test_health_reports_versions(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["commitment_schema_version"] == "audit-commit-v1"
    assert body["merkle_schema_version"] == "audit-merkle-v1"
    assert body["disclosure_schema_version"] == "audit-disclosure-v1"


def test_create_batch_response_is_public_only(client):
    body = _create(client)
    assert body["record_count"] == 3
    assert len(body["commitments"]) == 3 * 8
    for c in body["commitments"]:
        assert set(c) == {"leaf_index", "record_index", "field_position",
                          "field_name", "field_type", "commitment_hex"}
        assert "salt_hex" not in c and "value" not in c
    # Unsalted low-entropy field produces an explicit warning, not silence.
    assert any("UNSALTED_FIELD_ENUMERABLE" in w for w in body["warnings"])
    assert any("status" in w for w in body["warnings"])
    assert any("MISSING_FIELDS_COMMITTED" in w for w in body["warnings"])


def test_disclose_and_verify_happy_path(client):
    batch = _create(client)
    resp = client.post(f"/api/v1/batches/{batch['batch_id']}/disclose",
                       json={"selectors": [
                           {"record_index": 0, "field_name": "merchant"},
                           {"record_index": 2, "field_name": "quantity"},
                           {"record_index": 1, "field_name": "status"}]},
                       headers=RUN_HEADER)
    assert resp.status_code == 200, resp.text
    pkg = resp.json()
    assert pkg["batch_id"] == batch["batch_id"]
    assert pkg["root_hex"] == batch["root_hex"]

    verified = client.post("/api/v1/verify", json={"package": pkg}, headers=RUN_HEADER)
    assert verified.status_code == 200
    verdict = verified.json()
    assert verdict["valid"] is True
    assert verdict["verdict"] == "VALID"
    assert verdict["root_hex"] == verdict["recomputed_root_hex"]


def test_verify_endpoint_reports_specific_failure_for_wrong_salt(client):
    batch = _create(client)
    pkg = client.post(f"/api/v1/batches/{batch['batch_id']}/disclose",
                      json={"selectors": [{"record_index": 0, "field_name": "merchant"}]},
                      headers=RUN_HEADER).json()
    pkg["disclosed"][0]["salt_hex"] = "00" * 16
    verdict = client.post("/api/v1/verify", json={"package": pkg},
                          headers=RUN_HEADER).json()
    assert verdict["valid"] is False
    assert verdict["verdict"] == "COMMITMENT_MISMATCH"
    item = verdict["items"][0]
    assert item["verdict"] == "COMMITMENT_MISMATCH"
    assert item["detail"]["server_diagnosis"] == "WRONG_SALT"


def test_verify_endpoint_reports_identity_substitution(client):
    batch = _create(client)
    pkg = client.post(f"/api/v1/batches/{batch['batch_id']}/disclose",
                      json={"selectors": [
                          {"record_index": 0, "field_name": "merchant"},
                          {"record_index": 1, "field_name": "merchant"}]},
                      headers=RUN_HEADER).json()
    # Re-label record-1's item as record 0 but keep its own commitment/path:
    item = pkg["disclosed"][1]
    item["record_index"] = 0
    # now a duplicate identity exists in disclosed -> CELL_SET or identity mismatch
    verdict = client.post("/api/v1/verify", json={"package": pkg},
                          headers=RUN_HEADER).json()
    assert verdict["valid"] is False
    assert verdict["verdict"] in ("CELL_SET_MISMATCH", "FIELD_IDENTITY_MISMATCH")


def test_error_codes_are_specific_not_generic_200(client):
    missing = client.post("/api/v1/batches/does-not-exist/disclose",
                          json={"selectors": [{"record_index": 0, "field_name": "x"}]},
                          headers=RUN_HEADER)
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "BATCH_NOT_FOUND"

    batch = _create(client)
    unknown_field = client.post(
        f"/api/v1/batches/{batch['batch_id']}/disclose",
        json={"selectors": [{"record_index": 0, "field_name": "ghost"}]},
        headers=RUN_HEADER)
    assert unknown_field.status_code == 404
    assert unknown_field.json()["detail"]["code"] == "UNKNOWN_FIELD"

    malformed = client.post(
        f"/api/v1/batches/{batch['batch_id']}/disclose",
        json={"selectors": []}, headers=RUN_HEADER)
    assert malformed.status_code == 422  # pydantic min_length

    bad_encode = client.post("/api/v1/batches", json={
        "schema": [{"name": "qty", "type": "int"}],
        "records": [{"qty": "not-a-number"}]}, headers=RUN_HEADER)
    assert bad_encode.status_code == 422
    assert bad_encode.json()["detail"]["code"] == "ENCODE_ERROR"


def test_audit_endpoint_correlates_run_id_and_keeps_failure_verdicts(client):
    _create(client)
    client.post("/api/v1/batches/missing/disclose",
                json={"selectors": [{"record_index": 0, "field_name": "x"}]},
                headers=RUN_HEADER)
    rows = client.get("/api/v1/audit", headers=RUN_HEADER).json()
    run_rows = [r for r in rows if r["run_id"] == "run::http-acceptance"]
    actions = {(r["action"], r["verdict"]) for r in run_rows}
    assert ("batch.create", "CREATED") in actions
    assert ("disclosure.issue", "BATCH_NOT_FOUND") in actions
    # No failure was ever logged as a success.
    assert all(r["verdict"] != "SUCCESS" for r in run_rows)


def test_state_isolation_between_databases(tmp_path, client, settings):
    # The client DB must not see batches written to a separate database file.
    from app.storage.db import Database

    other = Database(str(tmp_path / "other.db"))
    from app.config import Settings as S
    from app.services.batch_service import BatchService
    BatchService(other, S(db_path=other.path, audit_log_path="logs/other.log"),
                 "run::other").create_batch(
        fixture_schema(), fixture_records(), batch_id="batch-other")
    other.close()

    resp = client.get("/api/v1/batches/batch-other", headers=RUN_HEADER)
    assert resp.status_code == 404
