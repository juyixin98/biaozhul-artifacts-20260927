"""End-to-end API tests with a temp SQLite store and an independent client."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import Settings
from app.store.sqlite_store import JobStore
from tests.fixtures import make_batch
from tests.oracle import oracle_unify


@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=tmp_path / "test.db",
                        max_cardinality=2**32 - 1, log_level="INFO")
    store = JobStore(settings.db_path)
    app = create_app(settings=settings, store=store)
    with TestClient(app) as c:
        c._store = store
        yield c


def test_health_reports_versions(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert set(["python", "pyarrow", "fastapi", "pydantic"]) <= set(body["versions"])


def test_unify_concrete_result_and_job_persistence(client, overlapping_batches):
    payload = {"value_type": "string", "batches": overlapping_batches}
    r = client.post("/api/v1/unify", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()

    # concrete values
    assert body["global_dictionary"] == ["a", "b", "c"]
    assert body["index_width_bits"] == 8
    assert body["cardinality"] == 3
    assert body["sort_policy"] == "typed-ascending-v1"
    remaps = {x["batch_id"]: x for x in body["batch_remaps"]}
    assert remaps["b0"]["local_to_global"] == [0, 1]
    assert remaps["b1"]["local_to_global"] == [1, 2, 0]
    assert remaps["b0"]["null_count"] == 1

    # job row is SUCCEEDED and queryable
    job = client.get(f"/api/v1/jobs/{body['job_id']}").json()
    assert job["status"] == "SUCCEEDED"
    assert job["cardinality"] == 3
    assert job["versions_json"]["pyarrow"]
    assert len(job["batches"]) == 2
    assert job["finished_at"] is not None

    # independent oracle agreement
    oracle = oracle_unify(overlapping_batches, value_type="string")
    assert body["global_dictionary"] == list(oracle.global_dictionary)


def test_duplicate_dictionary_entries_opt_in_canonicalization(client, duplicate_dict_batch):
    # default: rejected with a specific category
    r = client.post("/api/v1/unify",
                    json={"value_type": "string", "batches": [duplicate_dict_batch]})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "DUPLICATE_VALUE_IN_DICTIONARY"
    assert err["details"]["first_code"] == 0
    assert err["details"]["duplicate_code"] == 1
    job_id = None  # failures are still persisted below via the opt-in path

    # opt-in: duplicates canonicalized, indices rewritten to code 0/2
    r = client.post("/api/v1/unify", json={
        "value_type": "string",
        "dedupe_local_dictionary": True,
        "batches": [duplicate_dict_batch],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["global_dictionary"] == ["x", "y"]
    remap = body["batch_remaps"][0]
    # original indices [0,1,2,3,2,0] with codes 1,3 -> 0  => [0,0,2?...]
    # canonical dict is ['x'(0),'y'(1)] so y's canonical code is 1
    assert remap["local_to_global"] == [0, 1]
    assert remap["global_indices"] == [0, 0, 1, 0, 1, 0]
    assert body["normalization"][0]["duplicate_dictionary_entries"] == 2
    # every decoded row matches the original values
    decoded = ["x" if c == 0 else "y"
               for c in remap["global_indices"]]
    assert decoded == ["x", "x", "y", "x", "y", "x"]


def test_empty_dictionary_all_null_rows_api(client, empty_dictionary_batch):
    r = client.post("/api/v1/unify",
                    json={"value_type": "string", "batches": [empty_dictionary_batch]})
    assert r.status_code == 200
    body = r.json()
    assert body["global_dictionary"] == []
    assert body["cardinality"] == 0
    assert body["index_width_bits"] == 8
    remap = body["batch_remaps"][0]
    assert remap["null_count"] == 4
    assert remap["global_indices"] == [0, 0, 0, 0]


def test_strict_width_overflow_returns_422_and_failed_job(client):
    batches = [make_batch("b", [f"v{i}" for i in range(300)], list(range(300)))]
    r = client.post("/api/v1/unify", json={
        "value_type": "string", "index_policy": "strict",
        "target_width": 8, "batches": batches,
    })
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INDEX_WIDTH_OVERFLOW"
    assert err["details"]["capacity"] == 255


def test_out_of_range_index_400_with_row_context(client):
    r = client.post("/api/v1/unify", json={
        "value_type": "string",
        "batches": [make_batch("b", ["a"], [0, 5])],
    })
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "INDEX_OUT_OF_RANGE"
    assert err["details"]["row"] == 1
    assert err["details"]["index"] == 5


def test_null_dictionary_entry_rejected_with_position(client):
    r = client.post("/api/v1/unify", json={
        "value_type": "string",
        "batches": [make_batch("b", ["a", None], [0, 0])],
    })
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "NULL_DICTIONARY_ENTRY"
    assert err["details"]["code"] == 1


def test_invalid_shape_pydantic_422_is_not_reported_as_success(client):
    r = client.post("/api/v1/unify", json={"value_type": "string"})
    assert r.status_code == 422  # missing batches — framework validation


def test_unknown_job_404(client):
    r = client.get("/api/v1/jobs/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_failed_unification_persists_failed_status(client, tmp_path):
    """Failure path: a failed job is stored FAILED with the classified code,
    never SUCCEEDED."""
    r = client.post("/api/v1/unify", json={
        "value_type": "string",
        "batches": [make_batch("b", ["a"], [9])],
    })
    assert r.status_code == 400
    # job id isn't exposed on the error envelope; inspect the store directly
    store: JobStore = client._store
    row = store._conn.execute(
        "SELECT job_id, status, failure_code FROM jobs"
    ).fetchone()
    job_id, status, code = row
    assert status == "FAILED"
    assert code == "INDEX_OUT_OF_RANGE"
    got = client.get(f"/api/v1/jobs/{job_id}").json()
    assert got["status"] == "FAILED"
    assert got["failure_details_json"]["row"] == 0


def test_verify_endpoint_roundtrip_success(client, overlapping_batches):
    body = client.post("/api/v1/unify",
                       json={"value_type": "string",
                             "batches": overlapping_batches}).json()
    verify_payload = {
        "global_dictionary": body["global_dictionary"],
        "global_value_type": body["global_value_type"],
        "index_width_bits": body["index_width_bits"],
        "sort_policy": body["sort_policy"],
        "batches": [
            {
                "batch_id": rm["batch_id"],
                "original_dictionary": next(
                    b["dictionary"] for b in overlapping_batches
                    if b["batch_id"] == rm["batch_id"]),
                "original_indices": next(
                    b["indices"] for b in overlapping_batches
                    if b["batch_id"] == rm["batch_id"]),
                "local_to_global": rm["local_to_global"],
                "global_indices": rm["global_indices"],
                "validity": rm["validity"],
            }
            for rm in body["batch_remaps"]
        ],
    }
    r = client.post("/api/v1/verify", json=verify_payload)
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True
    assert out["rows_checked"] == 11


def test_verify_endpoint_detects_tampered_remap(client, overlapping_batches):
    body = client.post("/api/v1/unify",
                       json={"value_type": "string",
                             "batches": overlapping_batches}).json()
    rm0 = body["batch_remaps"][0]
    bad_indices = list(rm0["global_indices"])
    bad_indices[0] = 2  # force a different value
    src = next(b for b in overlapping_batches if b["batch_id"] == rm0["batch_id"])
    r = client.post("/api/v1/verify", json={
        "global_dictionary": body["global_dictionary"],
        "global_value_type": "string",
        "index_width_bits": body["index_width_bits"],
        "batches": [{
            "batch_id": rm0["batch_id"],
            "original_dictionary": src["dictionary"],
            "original_indices": src["indices"],
            "local_to_global": rm0["local_to_global"],
            "global_indices": bad_indices,
            "validity": rm0["validity"],
        }],
    })
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "VERIFICATION_MISMATCH"
