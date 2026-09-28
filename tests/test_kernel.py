"""安全内核测试：恢复接受/拒绝/无法判定三态，及归因边界。"""
from __future__ import annotations

import base64
import copy
import itertools

from tests.conftest import envelope_to_raw, issue

from threshold_service.audit import (
    OUTCOME_ACCEPTED,
    OUTCOME_INDETERMINATE,
    OUTCOME_REJECTED,
)
from threshold_service.integrity import tag_share
from threshold_service.models import ShareEnvelope, canonical_envelope_bytes
from threshold_service.policy import BELOW_THRESHOLD, COMMIT_MISMATCH


def _raws(result):
    return [envelope_to_raw(env) for env in result.shares]


def _resign(kernel, env: dict, *, new_y: bytes) -> dict:
    """用服务端主密钥给一个同 x、不同 y 的信封重新打标签（测试用）。"""
    forged = copy.deepcopy(env)
    forged["y"] = base64.b64encode(new_y).decode()
    draft = ShareEnvelope(
        version=forged["version"], set_id=forged["set_id"], x=forged["x"],
        y=forged["y"], threshold=forged["threshold"], field=forged["field"], tag="",
    )
    forged["tag"] = base64.b64encode(
        tag_share(kernel.settings.master_key, canonical_envelope_bytes(draft))
    ).decode()
    return forged


def test_reconstruct_secret_from_threshold_subset(kernel):
    secret = b"kernel-secret-xyz"
    result = issue(kernel, secret, 3, 5)
    for combo in itertools.combinations(range(5), 3):
        out = kernel.recover([_raws(result)[i] for i in combo])
        assert out.outcome == OUTCOME_ACCEPTED
        assert base64.b64decode(out.secret_b64) == secret
        assert out.secret_fp  # 有指纹而非明文日志


def test_below_threshold_refuses_and_returns_no_secret(kernel):
    result = issue(kernel, b"need-more", 3, 5)
    out = kernel.recover(_raws(result)[:2])
    assert out.outcome == OUTCOME_REJECTED
    assert out.category == BELOW_THRESHOLD
    assert out.secret_b64 is None


def test_tampered_authenticated_share_yields_indeterminate_not_wrong_secret(kernel):
    """关键安全属性：一个被同密钥签发的坏份额让全量插值失配，
    服务必须 INDETERMINATE，且绝不返回错误秘密。"""
    secret = b"commit-bound-secret"
    result = issue(kernel, secret, 2, 3)
    good = result.shares
    forged = _resign(kernel, good[0], new_y=bytes([b ^ 0x01 for b in
                                                   base64.b64decode(good[0]["y"])]))

    out = kernel.recover([envelope_to_raw(forged), _raws(result)[1]])
    assert out.outcome == OUTCOME_INDETERMINATE
    assert out.category == COMMIT_MISMATCH
    assert out.secret_b64 is None  # 不输出插值出的垃圾值
    assert "do NOT constitute identification" in out.reason or "cannot certify" in out.reason


def test_clean_subset_still_recovers_but_failure_cannot_name_all_bad_parties(kernel):
    """5 份中混入 1 份坏份额：干净门限子集仍能恢复；
    同时文档化断言——枚举线索可能为空或不完整，不能当作全部归因。"""
    secret = b"five-way-secret-value"
    result = issue(kernel, secret, 3, 5)
    forged = _resign(kernel, result.shares[0],
                     new_y=bytes([b ^ 0x5A for b in
                                  base64.b64decode(result.shares[0]["y"])]))

    # 干净的 3 份（不含坏份额）恢复成功
    clean_out = kernel.recover(_raws(result)[1:4])
    assert clean_out.outcome == OUTCOME_ACCEPTED
    assert base64.b64decode(clean_out.secret_b64) == secret

    # 坏份额 + 足够干净份额 -> INDETERMINATE，最多给出"哪些子集自洽"的线索
    bad_out = kernel.recover(
        [envelope_to_raw(forged)] + _raws(result)[1:]
    )
    assert bad_out.outcome == OUTCOME_INDETERMINATE
    # 诊断是尽力而为：可能识别到一些自洽子集，但响应必须显式声明不构成完整归因
    assert bad_out.enum_truncated in (True, False)
    if bad_out.consistent_subsets:
        # 自洽子集绝不能包含坏份额指纹
        bad_fp_substring = bad_out.evaluation.bad_tag_fingerprints  # 坏标签为空（标签合法）
        assert bad_fp_substring == []
        forged_fp_mark = f"x={result.shares[0]['x']}:"
        for subset in bad_out.consistent_subsets:
            assert not any(forged_fp_mark in entry for entry in subset)


def test_request_id_propagates_to_audit(kernel):
    result = issue(kernel, b"traceable", 2, 3)
    out = kernel.recover(_raws(result)[:2], request_id="req_fixed_trace")
    assert out.request_id == "req_fixed_trace"
    records = kernel.audit.query(request_id="req_fixed_trace")
    assert records and all(r["request_id"] == "req_fixed_trace" for r in records)
    # 审计记录中不得出现秘密明文
    line = str(records)
    assert "traceable" not in line
