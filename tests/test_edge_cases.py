"""边界与防御分支补充测试。"""
from __future__ import annotations

import base64
import copy

import pytest

from tests.conftest import envelope_to_raw, issue

from threshold_service.audit import OUTCOME_INDETERMINATE, OUTCOME_REJECTED
from threshold_service.integrity import tag_share
from threshold_service.models import ShareEnvelope, canonical_envelope_bytes
from threshold_service.policy import BELOW_THRESHOLD, COMMIT_MISMATCH
from threshold_service.shamir import ShareError


def test_kernel_rejects_empty_secret_and_bad_dimensions(kernel):
    with pytest.raises(ShareError):
        kernel.issue_set(secret=b"", threshold=2, share_count=3)
    with pytest.raises(ShareError):
        kernel.issue_set(secret=b"x", threshold=4, share_count=3)
    with pytest.raises(ShareError):
        kernel.issue_set(secret=b"x", threshold=2, share_count=256)
    records = kernel.audit.query()
    assert any(r["outcome"] == OUTCOME_REJECTED for r in records)


def test_non_dict_json_is_malformed(kernel):
    result = issue(kernel, b"notdict", 2, 3)
    out = kernel.recover(["[1,2,3]", envelope_to_raw(result.shares[0])])
    assert out.category == BELOW_THRESHOLD
    assert out.evaluation.malformed[0]["reason"]


def test_non_base64_tag_is_malformed_evidence(kernel):
    """标签本身连 base64 都不是 -> 解析阶段即 MALFORMED（不拖到标签比对）。"""
    result = issue(kernel, b"badtagbytes", 2, 3)
    env = copy.deepcopy(result.shares[0])
    env["tag"] = "@@@not-base64@@@"
    out = kernel.recover([envelope_to_raw(env), envelope_to_raw(result.shares[1])])
    assert out.category == BELOW_THRESHOLD  # 该证据被剔除后只剩 1 个可用
    assert len(out.evaluation.malformed) == 1
    assert "base64" in out.evaluation.malformed[0]["reason"]


def test_enum_truncation_flag_when_many_shares(kernel, monkeypatch):
    """份额数较多时枚举预算耗尽 -> enum_truncated=True，结论仍为无法判定。"""
    import threshold_service.kernel as kernel_mod

    monkeypatch.setattr(kernel_mod, "SUBSET_ENUM_BUDGET", 5)
    result = issue(kernel, b"big-set-secret!!", 3, 8)

    forged = copy.deepcopy(result.shares[0])
    flipped = bytes(a ^ 0x77 for a in base64.b64decode(forged["y"]))
    forged["y"] = base64.b64encode(flipped).decode()
    draft = ShareEnvelope(
        version=forged["version"], set_id=forged["set_id"], x=forged["x"],
        y=forged["y"], threshold=forged["threshold"], field=forged["field"], tag="",
    )
    forged["tag"] = base64.b64encode(
        tag_share(kernel.settings.master_key, canonical_envelope_bytes(draft))
    ).decode()

    raws = [envelope_to_raw(forged)] + [
        envelope_to_raw(s) for s in result.shares[1:]
    ]
    out = kernel.recover(raws)
    assert out.outcome == OUTCOME_INDETERMINATE
    assert out.category == COMMIT_MISMATCH
    assert out.enum_truncated is True
    assert out.secret_b64 is None


def test_http_empty_secret_bad_base64_and_set_404(client):
    # 空秘密
    r = client.post("/sets", json={"secret_b64": "", "threshold": 2, "share_count": 3})
    assert r.status_code == 422
    # 坏 base64
    r = client.post("/sets", json={"secret_b64": "@@@", "threshold": 2, "share_count": 3})
    assert r.status_code == 422
    # 未知集合
    assert client.get("/sets/nope").status_code == 404
    # 列表端点
    listed = client.get("/sets")
    assert listed.status_code == 200
    assert isinstance(listed.json(), list)
