"""End-to-end service tests: failure categories, steps, uncertainties."""
import pytest

from app.service import ValidationService

from .fixtures import (
    EMPTY_STRUCT_RECORDS, NESTED_LIST_RECORDS, SCHEMA_EMPTY_STRUCT,
    SCHEMA_NESTED_LIST, UNSUPPORTED_SCHEMAS, cross_page_records,
)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("PNV_DB_PATH", str(tmp_path / "v.db"))
    monkeypatch.setenv("PNV_ARTIFACT_DIR", str(tmp_path / "art"))
    from app.config import get_settings
    import app.config as config_mod
    # force fresh settings/store bound to the temp paths
    settings = get_settings()
    from app.storage.metadata import MetadataStore
    return ValidationService(MetadataStore(settings.db_path))


def _nested_schema():
    return SCHEMA_NESTED_LIST


def test_passing_validation_records_steps_and_pages(service):
    out = service.validate("r1", _nested_schema(), NESTED_LIST_RECORDS,
                           expected_tree=NESTED_LIST_RECORDS,
                           force_page_after_records=3)
    assert out.status == "passed"
    assert out.mismatches == []
    step_names = [s.name for s in out.steps]
    assert "schema_parsed" in step_names
    assert "levels_encoded" in step_names
    assert "self_roundtrip" in step_names
    assert "oracle_roundtrip" in step_names
    assert "cross_interop" in step_names
    assert out.page_count >= 1
    assert out.artifact["self_parquet"]["bytes"] > 0
    # PyArrow's independent file was also produced and compared.
    assert out.artifact["oracle_parquet"]["bytes"] > 0


def test_expected_tree_mismatch_is_named_category(service):
    wrong = [dict(r) for r in NESTED_LIST_RECORDS]
    wrong[2] = dict(wrong[2])
    wrong[2]["ids"] = [[1]]  # hand-written expected tree is wrong
    out = service.validate("r2", _nested_schema(), NESTED_LIST_RECORDS,
                           expected_tree=wrong)
    assert out.status == "failed"
    assert out.error_category == "EXPECTED_TREE_MISMATCH"
    mm = out.mismatches[0]
    assert mm["record_index"] == 2
    assert "ids" in mm["column_path"]


def test_unsupported_schema_returns_category_without_writing(service):
    out = service.validate("r3", UNSUPPORTED_SCHEMAS[0], [])
    assert out.status == "error"
    assert out.error_category == "UNSUPPORTED_LOGICAL_TYPE"
    assert "decimal" in out.error_message.lower()


def test_invalid_required_null_record(service):
    schema = {"fields": [
        {"name": "id", "type": "int64", "repetition": "required"}]}
    out = service.validate("r4", schema, [{"id": None}])
    assert out.status == "error"
    assert out.error_category == "INVALID_RECORD"


def test_cross_page_large_volume_matches_oracle(service):
    records = cross_page_records(200)
    out = service.validate("r5", _nested_schema(), records,
                           expected_tree=records,
                           force_page_after_records=7)
    assert out.status == "passed", out.mismatches
    # many pages across leaves
    assert out.page_count > 10


def test_empty_struct_uncertainty_is_separated(service):
    out = service.validate("r6", SCHEMA_EMPTY_STRUCT, EMPTY_STRUCT_RECORDS,
                           expected_tree=EMPTY_STRUCT_RECORDS)
    assert out.status == "passed"
    assert any("Zero-field struct" in u for u in out.uncertainties)
    # uncertainties must not be mixed into hard failures/warnings
    assert out.mismatches == []


def test_request_persisted_and_retrievable(service):
    service.validate("r7", _nested_schema(), NESTED_LIST_RECORDS)
    row = service.store.get_request("r7")
    assert row["status"] == "passed"
    assert row["record_count"] == len(NESTED_LIST_RECORDS)
    assert len(row["steps"]) >= 5
    assert row["artifact"]["self_path"].endswith("r7.self.parquet")


def test_failure_also_persisted_with_category(service):
    out = service.validate("r8", _nested_schema(), NESTED_LIST_RECORDS,
                           expected_tree=[{"id": 1, "ids": [], "tags": []}])
    assert out.status == "failed"
    row = service.store.get_request("r8")
    assert row["status"] == "failed"
    assert row["error_category"] == "EXPECTED_TREE_MISMATCH"
