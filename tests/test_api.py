"""HTTP 接口测试：状态码、reason_code、X-Request-ID、import 白名单。"""

from __future__ import annotations

from lake_txn import errors
from lake_txn.format_adapter import ColumnSpec, write_parquet_atomic
from tests.conftest import COLUMNS, TABLE


def test_error_envelope_and_request_id(client):
    r = client.post(
        "/v1/commits",
        json={"table": "ghost", "request_id": "x", "kind": "APPEND",
              "base_snapshot_id": 0, "files": ["x-0"]},
        headers={"X-Request-ID": "corr-123"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"] is True
    assert body["reason_code"] == errors.UNKNOWN_TABLE
    assert r.headers["x-request-id"] == "corr-123"


def test_conflict_returns_409_with_category_and_key_state(client):
    def append(rid, base):
        client.post(
            "/v1/staging/files",
            json={"table": TABLE, "request_id": rid,
                  "files": [{"logical_name": f"{rid}-0", "mode": "inline",
                             "records": [{"order_id": 1, "region": "cn", "amount": 1.0}]}]},
        )
        return client.post(
            "/v1/commits",
            json={"table": TABLE, "request_id": rid, "kind": "APPEND",
                  "base_snapshot_id": base, "files": [f"{rid}-0"]},
        )

    first = append("a", 0)
    assert first.status_code == 200
    clash = append("b", 0)
    assert clash.status_code == 409
    body = clash.json()
    assert body["reason_code"] == errors.PARTITION_CONFLICT
    assert body["detail"]["conflict_partitions"] == ["cn"]
    assert body["detail"]["head_snapshot_id"] == 1


def test_partition_column_not_in_schema_is_domain_400(client):
    # 分区列不在列定义中 -> 服务层领域校验（pydantic 只校验形状）
    r = client.post(
        "/v1/tables",
        json={"table": "bad", "columns": [{"name": "x", "type": "int64"}],
              "partition_column": "missing"},
    )
    assert r.status_code == 400
    assert r.json()["reason_code"] == errors.VALIDATION_ERROR


def test_pydantic_shape_error_is_422(client):
    # 枚举不合法 -> pydantic 形状校验
    r = client.post(
        "/v1/commits",
        json={"table": TABLE, "request_id": "x", "kind": "UPSERT",
              "base_snapshot_id": 0, "files": ["x"]},
    )
    assert r.status_code == 422
    assert r.json()["reason_code"] == errors.VALIDATION_ERROR

    # 负数基线 -> pydantic 形状校验
    r = client.post(
        "/v1/commits",
        json={"table": TABLE, "request_id": "x", "kind": "APPEND",
              "base_snapshot_id": -1, "files": ["x"]},
    )
    assert r.status_code == 422


def test_unknown_commit_status_endpoint(client):
    r = client.get("/v1/commits/phantom")
    assert r.status_code == 400
    assert r.json()["reason_code"] == "UNKNOWN_REQUEST"


def test_import_mode_from_whitelisted_inbound(tmp_path):
    from fastapi.testclient import TestClient

    from lake_txn.api import create_app
    from tests.conftest import make_settings

    inbound = tmp_path / "inbound"
    inbound.mkdir()
    specs = [ColumnSpec(c["name"], c["type"]) for c in COLUMNS]
    src = write_parquet_atomic(
        [{"order_id": 7, "region": "cn", "amount": 3.5}],
        specs, inbound, "external.parquet",
    )
    settings = make_settings(tmp_path, inbound=(inbound,))
    app = create_app(settings)
    client = TestClient(app)
    client.post("/v1/tables", json={"table": TABLE, "columns": COLUMNS, "partition_column": "region"})

    r = client.post(
        "/v1/staging/files",
        json={"table": TABLE, "request_id": "imp",
              "files": [{"logical_name": "ext", "mode": "import",
                         "source_path": str(src.path), "declared_partition": "cn",
                         "declared_sha256": src.sha256}]},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/v1/commits",
        json={"table": TABLE, "request_id": "imp", "kind": "APPEND",
              "base_snapshot_id": 0, "files": ["ext"]},
    )
    assert r.status_code == 200
    snap = client.get(f"/v1/tables/{TABLE}/snapshots/1").json()
    assert snap["files"][0]["row_count"] == 1


def test_import_path_outside_whitelist_rejected(tmp_path):
    from fastapi.testclient import TestClient

    from lake_txn.api import create_app
    from tests.conftest import make_settings

    allowed = tmp_path / "inbound"
    allowed.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    evil = outside / "e.parquet"
    specs = [ColumnSpec(c["name"], c["type"]) for c in COLUMNS]
    write_parquet_atomic(
        [{"order_id": 1, "region": "cn", "amount": 1.0}], specs, outside, "e.parquet"
    )
    settings = make_settings(tmp_path, inbound=(allowed,))
    client = TestClient(create_app(settings))
    client.post("/v1/tables", json={"table": TABLE, "columns": COLUMNS, "partition_column": "region"})

    r = client.post(
        "/v1/staging/files",
        json={"table": TABLE, "request_id": "imp",
              "files": [{"logical_name": "ext", "mode": "import",
                         "source_path": str(evil), "declared_partition": "cn"}]},
    )
    assert r.status_code == 422
    body = r.json()
    # 整个暂存请求被拒；逐文件失败明细保留具体类别
    assert body["reason_code"] == errors.STAGE_VALIDATION_FAILED
    assert body["detail"]["failures"][0]["reason_code"] == errors.IMPORT_PATH_FORBIDDEN
