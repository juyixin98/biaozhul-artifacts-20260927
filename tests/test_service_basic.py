"""服务级测试：用独立物理 oracle 断言每个快照的**具体行集**与元数据。

oracle 不经被测服务读数据（见 conftest.IndependentOracle）。
"""

from __future__ import annotations

import pytest

from lake_txn import errors
from tests.conftest import PARTITION_COL, TABLE, stage_inline


def test_empty_table_snapshot_zero_has_no_rows(service, oracle):
    assert oracle.head_snapshot(TABLE) == 0
    assert oracle.rows_of(TABLE, 0) == []
    assert oracle.manifest(TABLE, 0) == []


def test_first_append_attaches_file_and_rows_match(service, oracle):
    stage_inline(service, "r1", "cn", [1, 2])
    res = service.commit(TABLE, "r1", "APPEND", 0, ["r1-0"])
    assert res["status"] == "ACCEPTED"
    assert res["snapshot_id"] == 1
    assert res["merged"] is False

    rows = oracle.rows_of(TABLE, 1)
    assert sorted(r["order_id"] for r in rows) == [1, 2]
    assert {r[PARTITION_COL] for r in rows} == {"cn"}
    # 数据物理文件名是内容指纹
    files = oracle.manifest(TABLE, 1)
    assert len(files) == 1
    assert files[0]["path"].endswith(f"{files[0]['sha256']}.parquet")


def test_append_chain_union_rows(service, oracle):
    stage_inline(service, "r1", "cn", [1])
    service.commit(TABLE, "r1", "APPEND", 0, ["r1-0"])
    stage_inline(service, "r2", "us", [2])
    res = service.commit(TABLE, "r2", "APPEND", 1, ["r2-0"])
    assert res["snapshot_id"] == 2

    snap1 = oracle.rows_by_partition(TABLE, 1)
    snap2 = oracle.rows_by_partition(TABLE, 2)
    assert set(snap1) == {"cn"}
    assert set(snap2) == {"cn", "us"}
    assert [r["order_id"] for r in snap2["cn"]] == [1]
    assert [r["order_id"] for r in snap2["us"]] == [2]
    # 旧快照不可变：snapshot 1 行集不随 snapshot 2 改变
    assert oracle.rows_of(TABLE, 1) == snap1["cn"]


def test_overwrite_replaces_only_named_partition_rows(service, oracle):
    stage_inline(service, "r1", "cn", [1])
    service.commit(TABLE, "r1", "APPEND", 0, ["r1-0"])
    stage_inline(service, "r2", "us", [2])
    service.commit(TABLE, "r2", "APPEND", 1, ["r2-0"])

    # 覆盖 cn 分区为新行 100；us 必须原样保留
    stage_inline(service, "r3", "cn", [100], amount=99.0)
    res = service.commit(TABLE, "r3", "OVERWRITE", 2, ["r3-0"], drop_partitions=["cn"])
    assert res["status"] == "ACCEPTED"
    assert res["removed_files"] == 1
    assert res["added_files"] == 1

    snap3 = oracle.rows_by_partition(TABLE, 3)
    assert [r["order_id"] for r in snap3["cn"]] == [100]
    assert snap3["cn"][0]["amount"] == 99.0
    assert [r["order_id"] for r in snap3["us"]] == [2]

    # 旧快照 2 仍能读出被覆盖前的 cn 行（不可变快照）
    snap2_rows = oracle.rows_of(TABLE, 2)
    assert sorted(r["order_id"] for r in snap2_rows) == [1, 2]


def test_overwrite_requires_drop_partitions(service):
    stage_inline(service, "r1", "cn", [1])
    with pytest.raises(errors.DomainError) as ei:
        service.commit(TABLE, "r1", "OVERWRITE", 0, ["r1-0"])
    assert ei.value.reason_code == errors.VALIDATION_ERROR


def test_append_cannot_drop_partitions(service):
    stage_inline(service, "r1", "cn", [1])
    with pytest.raises(errors.DomainError) as ei:
        service.commit(TABLE, "r1", "APPEND", 0, ["r1-0"], drop_partitions=["cn"])
    assert ei.value.reason_code == errors.VALIDATION_ERROR


def test_overwrite_new_files_must_lie_in_drop_scope(service):
    stage_inline(service, "r1", "cn", [1])
    service.commit(TABLE, "r1", "APPEND", 0, ["r1-0"])
    stage_inline(service, "r2", "us", [2])
    with pytest.raises(errors.DomainError) as ei:
        service.commit(TABLE, "r2", "OVERWRITE", 1, ["r2-0"], drop_partitions=["cn"])
    assert ei.value.reason_code == errors.VALIDATION_ERROR


def test_unknown_base_and_unknown_table(service):
    stage_inline(service, "r1", "cn", [1])
    with pytest.raises(errors.DomainError) as ei:
        service.commit(TABLE, "r1", "APPEND", 99, ["r1-0"])
    assert ei.value.reason_code == errors.UNKNOWN_BASE

    with pytest.raises(errors.DomainError) as ei:
        service.commit("nope", "x", "APPEND", 0, ["x-0"])
    assert ei.value.reason_code == errors.UNKNOWN_TABLE


def test_staged_file_tampered_before_commit_rejected(service, settings):
    stage_inline(service, "r1", "cn", [1])
    # 直接篡改已暂存的物理文件
    staged = list((settings.staging_dir / "r1").glob("*.parquet"))[0]
    staged.write_bytes(b"tampered-not-parquet")
    with pytest.raises(errors.DomainError) as ei:
        service.commit(TABLE, "r1", "APPEND", 0, ["r1-0"])
    # 指纹变化先被发现
    assert ei.value.reason_code == errors.FILE_HASH_MISMATCH


def test_files_fully_staged_before_snapshot_exists(service, settings, oracle):
    # 暂存成功但未提交：没有任何快照，数据只在暂存区
    stage_inline(service, "r1", "cn", [1])
    assert oracle.head_snapshot(TABLE) == 0
    assert list((settings.staging_dir / "r1").glob("*.parquet"))
    data_files = list(settings.root.joinpath("tables", TABLE, "data").rglob("*.parquet"))
    assert data_files == []
