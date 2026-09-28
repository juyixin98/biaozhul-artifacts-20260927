"""Reproducibility/logging tests: logs correlate to run identity and show
versions, steps and the verdict basis."""
from __future__ import annotations

from . import fixtures
from .conftest import assert_error


def test_run_log_contains_identity_versions_steps_and_verdict(
        client, make_payload, settings):
    refs = fixtures.repeated_dict_items()
    resp = client.post("/v1/encode",
                       json=make_payload(refs, run_id="logged-42"))
    assert resp.status_code == 200
    log = (settings.log_dir + "/run-logged-42.log")
    text = open(log, encoding="utf-8").read()
    assert "run_id=logged-42" in text
    assert "pyarrow" in text and "fastapi" in text and "sqlite" in text
    assert "batch_validated" in text
    assert "global_merged" in text
    assert "width_decided" in text
    assert "roundtrip" in text
    assert "VERDICT ok=True basis=roundtrip all rows matched" in text


def test_failed_run_log_records_category_not_fake_success(
        client, make_payload, settings):
    refs = fixtures.overflow_257()
    assert_error(client.post("/v1/encode",
                             json=make_payload(refs, run_id="overflow-log")),
                 422, "CARDINALITY_OVERFLOW")
    text = open(settings.log_dir + "/run-overflow-log.log",
                encoding="utf-8").read()
    assert "VERDICT ok=False" in text
    assert "error category=CARDINALITY_OVERFLOW" in text
    # The events up to the failure are present, showing computation steps.
    assert "global_merged" in text and "cardinality" in text


def test_unknown_paths_do_not_return_success(client):
    assert client.get("/v1/runs/does-not-exist").status_code == 404
    malformed = client.post("/v1/encode", content=b"not-json",
                            headers={"content-type": "application/json"})
    assert_error(malformed, 400, "REQUEST_MALFORMED")
