"""状态隔离与审计层测试。"""
from __future__ import annotations

import json

from tests.conftest import issue

from threshold_service.audit import OUTCOME_REJECTED


def test_shares_are_encrypted_at_rest(kernel, settings, tmp_path):
    result = issue(kernel, b"rest-encrypted!", 2, 3)
    # 直接读 SQLite 文件，明文段不得出现
    raw_db = open(settings.database_path, "rb").read()
    assert b"rest-encrypted!" not in raw_db
    # y 明文（base64 信封形式）也不应裸露
    for env in result.shares:
        assert env["y"].encode() not in raw_db


def test_sets_are_isolated_and_unique_x_enforced(kernel):
    a = issue(kernel, b"set-A-isolated", 2, 3)
    b = issue(kernel, b"set-B-isolated", 2, 3)
    assert a.set_id != b.set_id
    assert kernel.repo.get_set(a.set_id) is not None
    assert kernel.repo.get_set(b.set_id) is not None
    fps_a = kernel.repo.share_fingerprints(a.set_id)
    fps_b = kernel.repo.share_fingerprints(b.set_id)
    assert fps_a and fps_b and not (fps_a & fps_b)


def test_two_databases_do_not_share_state(settings, tmp_path):
    from threshold_service.app import _State

    s1 = _State(settings)
    result = s1.kernel.issue_set(secret=b"only-in-db1", threshold=2, share_count=3)
    other_settings = type(settings)(
        master_key=settings.master_key,
        database_path=str(tmp_path / "other" / "tss.db"),
        audit_path=str(tmp_path / "other" / "audit.log"),
        env=settings.env,
    )
    s2 = _State(other_settings)
    assert s2.repo.get_set(result.set_id) is None


def test_audit_redaction_drops_sensitive_keys(kernel):
    kernel.audit.record(
        request_id="req_redact", stage="test", outcome=OUTCOME_REJECTED,
        reason="probe",
        detail={
            "set_id": "s1", "threshold": 2,
            "secret": b"should-never-appear",
            "master_key": "leak", "random_extra": "x",
            "accepted_fingerprints": ["sha256:abcd"],
            "candidate_secret_fp": "sha256:feed",
        },
    )
    records = kernel.audit.query(request_id="req_redact")
    assert len(records) == 1
    detail = records[0]["detail"]
    assert detail["set_id"] == "s1"
    assert detail["accepted_fingerprints"] == ["sha256:abcd"]
    assert detail["candidate_secret_fp"] == "sha256:feed"  # 指纹放行
    assert "secret" not in detail and "master_key" not in detail
    assert "random_extra" not in detail
    blob = json.dumps(records)
    assert "should-never-appear" not in blob and "leak" not in blob
