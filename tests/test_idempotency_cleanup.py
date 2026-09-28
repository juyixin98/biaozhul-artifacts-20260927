"""提交响应丢失（幂等重放）、请求复用、孤立文件清扫、发布失败清理、脱敏。"""

from __future__ import annotations

import pytest

from lake_txn import errors
from tests.conftest import TABLE, make_old, stage_inline


# ---------- 提交响应丢失 ----------
def test_lost_commit_response_retry_returns_same_snapshot(service, oracle):
    stage_inline(service, "r1", "cn", [1, 2])
    first = service.commit(TABLE, "r1", "APPEND", 0, ["r1-0"])
    snap_after_first = oracle.head_snapshot(TABLE)

    # 客户端没收到响应：用完全相同的 request_id/请求体重试
    retry = service.commit(TABLE, "r1", "APPEND", 0, ["r1-0"])
    assert retry["status"] == "ACCEPTED"
    assert retry["snapshot_id"] == first["snapshot_id"]
    assert retry["idempotent_replay"] is True
    # 没有产生第二个快照、行没有翻倍
    assert oracle.head_snapshot(TABLE) == snap_after_first
    rows = oracle.rows_of(TABLE, 1)
    assert sorted(r["order_id"] for r in rows) == [1, 2]
    assert len(oracle.commit_log()) == 1  # 只有一条提交记录


def test_reuse_request_id_with_different_scope_rejected(service, oracle):
    stage_inline(service, "r1", "cn", [1])
    service.commit(TABLE, "r1", "APPEND", 0, ["r1-0"])

    stage_inline(service, "r2", "us", [2])
    # 拿旧 request_id 提交不同文件/不同类型
    with pytest.raises(errors.DomainError) as ei:
        service.commit(TABLE, "r1", "APPEND", 1, ["r2-0"])
    assert ei.value.reason_code == errors.REQUEST_SCOPE_MISMATCH


def test_commit_status_unknown_request_is_indeterminate(service):
    from lake_txn.errors import DomainError

    with pytest.raises(DomainError) as ei:
        service.get_commit_status("never-seen")
    assert ei.value.reason_code == "UNKNOWN_REQUEST"
    assert ei.value.status_code == 400


def test_commit_status_records_rejected_outcome(service):
    stage_inline(service, "a", "cn", [1])
    service.commit(TABLE, "a", "APPEND", 0, ["a-0"])
    stage_inline(service, "b", "cn", [2])
    with pytest.raises(errors.DomainError):
        service.commit(TABLE, "b", "APPEND", 0, ["b-0"])
    status = service.get_commit_status("b")
    assert status["status"] == "REJECTED"
    assert status["reason_code"] == errors.PARTITION_CONFLICT


# ---------- 暂存失败：每个失败文件独立清理记录 ----------
def test_failed_stage_files_quarantined_with_independent_ledger(service, settings, oracle):
    from lake_txn.service import StageFileInput

    # 文件 a 正常写出；文件 b 分区列含两个不同值（非法），整个暂存被拒
    good = StageFileInput(
        "good", "inline", [{"order_id": 1, "region": "cn", "amount": 1.0}]
    )
    bad = StageFileInput(
        "bad",
        "inline",
        [
            {"order_id": 2, "region": "cn", "amount": 1.0},
            {"order_id": 3, "region": "us", "amount": 1.0},
        ],
    )
    with pytest.raises(errors.DomainError) as ei:
        service.stage_files(TABLE, "req-x", [good, bad])
    assert ei.value.reason_code == errors.STAGE_VALIDATION_FAILED
    failure = ei.value.detail["failures"][0]
    assert failure["logical_name"] == "bad"
    assert failure["reason_code"] == errors.VALIDATION_ERROR

    # 已落盘的 good 文件必须被隔离（不残留在暂存区），且有独立台账记录
    assert not (settings.staging_dir / "req-x").exists()
    records = [r for r in oracle.cleanup_records() if r["request_id"] == "req-x"]
    assert len(records) == 1
    rec = records[0]
    assert rec["kind"] == "stage_failed"
    assert rec["status"] == "quarantined"
    assert rec["dest_path"] is not None
    assert (settings.root / rec["dest_path"]).exists()


def test_successful_commit_deletes_staging_and_logs_each_file(service, settings, oracle):
    stage_inline(service, "r1", "cn", [1])
    service.commit(TABLE, "r1", "APPEND", 0, ["r1-0"])
    assert not (settings.staging_dir / "r1").exists()
    records = [r for r in oracle.cleanup_records() if r["request_id"] == "r1"]
    assert len(records) == 1
    assert records[0]["kind"] == "stage_published"
    assert records[0]["status"] == "deleted"


# ---------- 孤立文件夹具 ----------
def test_sweep_removes_orphan_staging_dir_and_orphan_data_file(service, settings, oracle):
    # 夹具 1：从未登记的暂存目录（模拟客户端写到一半消失）
    orphan_stage = settings.staging_dir / "ghost-request"
    orphan_stage.mkdir(parents=True)
    ghost = orphan_stage / "ghost.parquet"
    ghost.write_bytes(b"ORPHAN")
    make_old(ghost)
    make_old(orphan_stage)

    # 夹具 2：数据区里一个不在任何快照清单中的 parquet（发布后未挂入）
    stage_inline(service, "real", "cn", [1])
    service.commit(TABLE, "real", "APPEND", 0, ["real-0"])
    data_dir = settings.data_dir(TABLE) / "cn"
    orphan_data = data_dir / "0000000000000000000000000000000000000000000000000000000000000000.parquet"
    orphan_data.write_bytes(b"ORPHAN-DATA")
    make_old(orphan_data)

    report = service.sweep(grace_seconds=0)
    assert report["quarantined_count"] == 2
    kinds = {r["kind"] for r in report["records"]}
    assert kinds == {"orphan_staging", "orphan_data"}

    # 文件进隔离区而不是直接删除，暂存空目录被清掉
    assert not ghost.exists()
    assert not orphan_stage.exists()
    assert not orphan_data.exists()
    quarantine_root = settings.quarantine_dir
    q_staging = list((quarantine_root / "orphan_staging").iterdir())
    q_data = list((quarantine_root / "orphan_data").iterdir())
    assert len(q_staging) == 1 and q_staging[0].read_bytes() == b"ORPHAN"
    assert len(q_data) == 1 and q_data[0].read_bytes() == b"ORPHAN-DATA"

    # 真正挂入快照的文件绝不能被动
    live = oracle.manifest(TABLE, 1)
    for m in live:
        assert (settings.root / m["path"]).exists()


def test_sweep_respects_grace_period_for_inflight_files(service, settings):
    inflight = settings.staging_dir / "inflight"
    inflight.mkdir(parents=True)
    f = inflight / "new.parquet"
    f.write_bytes(b"x")  # mtime 是“现在”
    report = service.sweep(grace_seconds=3600)
    assert report["quarantined_count"] == 0
    assert f.exists()


def test_sweep_touches_no_ready_registered_staging_files(service, settings):
    stage_inline(service, "ready-req", "cn", [1])  # ready 但未提交
    make_old(list((settings.staging_dir / "ready-req").glob("*.parquet"))[0])
    report = service.sweep(grace_seconds=0)
    assert report["quarantined_count"] == 0


# ---------- 发布失败 ----------
def test_publish_failure_records_each_file_and_no_snapshot(service, settings, monkeypatch, oracle):
    from lake_txn.service import LakeService

    stage_inline(service, "p1", "cn", [1])

    def boom(self, src, dest):
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr(LakeService, "_publish_one", boom)
    with pytest.raises(errors.DomainError) as ei:
        service.commit(TABLE, "p1", "APPEND", 0, ["p1-0"])
    assert ei.value.reason_code == errors.PUBLISH_FAILURE

    # 没有任何快照/清单产生
    assert oracle.head_snapshot(TABLE) == 0
    log = {r["request_id"]: r for r in oracle.commit_log()}
    assert log["p1"]["status"] == "PUBLISH_FAILED"
    assert log["p1"]["snapshot_id"] is None
    # 失败文件有独立清理记录
    records = [r for r in oracle.cleanup_records() if r["request_id"] == "p1"]
    assert len(records) == 1
    assert records[0]["reason_code"] == errors.PUBLISH_FAILURE


# ---------- 脱敏 ----------
def test_diagnostics_redacts_sensitive_fields(capsys, settings):
    from lake_txn.diagnostics import DiagnosticLogger, set_http_request_id

    set_http_request_id("req-redact-test")
    logger = DiagnosticLogger("test_redact", redact_fields=settings.redact_fields)
    logger.info(
        "stage_file_written",
        logical_name="f",
        sample={"order_id": 1, "region": "cn", "ssn": "110101199001011234",
                "meta": {"email": "alice@example.com", "amount": 1.0}},
    )
    captured = capsys.readouterr().err
    assert "110101199001011234" not in captured
    assert "alice@example.com" not in captured
    assert "REDACTED" in captured
    assert "req-redact-test" in captured  # 关联标识保留
    assert "amount" in captured  # 非敏感字段保留
