"""策略/证据解析测试：每种失败类别都有具体结果断言（非"接口可调"）。"""
from __future__ import annotations

import base64
import copy

from tests.conftest import envelope_to_raw, issue

from threshold_service.policy import (
    BELOW_THRESHOLD,
    MALFORMED_EVIDENCE,
    MIXED_SET,
    READY,
    REJECTED,
    UNKNOWN_SET,
    evaluate,
)


def _raws(result):
    return [envelope_to_raw(env) for env in result.shares]


def test_happy_path_ready(kernel):
    result = issue(kernel, b"policy-happy", 3, 5)
    ev = evaluate(_raws(result)[:3], kernel.repo, kernel.settings.master_key)
    assert ev.outcome == READY
    assert ev.distinct_x_count == 3
    assert len(ev.accepted_fingerprints) == 3


def test_below_threshold_is_rejected_with_category(kernel):
    result = issue(kernel, b"need-three", 3, 5)
    ev = evaluate(_raws(result)[:2], kernel.repo, kernel.settings.master_key)
    assert ev.outcome == REJECTED
    assert ev.category == BELOW_THRESHOLD
    assert ev.distinct_x_count == 2
    assert "3" in ev.reason


def test_duplicate_identical_share_not_counted_twice(kernel):
    """重复横坐标不得重复计数：同一份额交两次仍不足门限。"""
    result = issue(kernel, b"no-double-count", 3, 5)
    raws = _raws(result)
    ev = evaluate([raws[0], raws[0], raws[1]], kernel.repo,
                  kernel.settings.master_key)
    assert ev.outcome == REJECTED
    assert ev.category == BELOW_THRESHOLD
    assert ev.distinct_x_count == 2
    assert ev.repeated_fingerprints  # 重复被记录
    # 交够 3 个不同份额时，重复项不影响成功
    ev2 = evaluate([raws[0], raws[0], raws[1], raws[2]], kernel.repo,
                   kernel.settings.master_key)
    assert ev2.outcome == READY
    assert ev2.distinct_x_count == 3


def test_duplicate_x_with_conflicting_payload_drops_both(kernel):
    """同 x、等长、内容不同且都通过标签（模拟同主密钥下签发了冲突份额）：
    无法安全选择，冲突双方都排除，剩余份额不足门限。"""
    from threshold_service.integrity import tag_share
    from threshold_service.models import canonical_envelope_bytes, ShareEnvelope

    result = issue(kernel, b"conflict-x", 2, 3)
    env0 = copy.deepcopy(result.shares[0])

    forged = copy.deepcopy(env0)
    flipped = bytes(a ^ 0xFF for a in base64.b64decode(env0["y"]))
    forged["y"] = base64.b64encode(flipped).decode()
    draft = ShareEnvelope(
        version=forged["version"], set_id=forged["set_id"], x=forged["x"],
        y=forged["y"], threshold=forged["threshold"], field=forged["field"], tag="",
    )
    forged["tag"] = base64.b64encode(
        tag_share(kernel.settings.master_key, canonical_envelope_bytes(draft))
    ).decode()

    ev = evaluate(
        [envelope_to_raw(env0), envelope_to_raw(forged), _raws(result)[1]],
        kernel.repo, kernel.settings.master_key,
    )
    # threshold=2：冲突双方都被排除后只剩 1 个可用 -> 不足门限
    assert ev.category == BELOW_THRESHOLD
    assert ev.distinct_x_count == 1
    assert len(ev.duplicate_conflict_fingerprints) == 2


def test_mixed_set_is_hard_rejected(kernel):
    a = issue(kernel, b"set-A-secret", 2, 3)
    b = issue(kernel, b"set-B-secret", 2, 3)
    raws = [_raws(a)[0], _raws(b)[0]]
    ev = evaluate(raws, kernel.repo, kernel.settings.master_key)
    assert ev.outcome == REJECTED
    assert ev.category == MIXED_SET
    assert set(ev.unknown_sets) == {a.set_id, b.set_id} or ev.set_id in (a.set_id, b.set_id)


def test_unknown_set_rejected(kernel):
    result = issue(kernel, b"known", 2, 3)
    env0 = copy.deepcopy(result.shares[0])
    env1 = copy.deepcopy(result.shares[1])
    env0["set_id"] = "ghost-set"
    env1["set_id"] = "ghost-set"  # 同属一个未知集合，避免先命中 MIXED_SET
    ev = evaluate([envelope_to_raw(env0), envelope_to_raw(env1)],
                  kernel.repo, kernel.settings.master_key)
    assert ev.category == UNKNOWN_SET
    assert ev.unknown_sets == ["ghost-set"]


def test_incompatible_field_params_excluded(kernel):
    result = issue(kernel, b"field-bind", 2, 3)
    env = copy.deepcopy(result.shares[0])
    env["field"] = {"bits": 8, "generator": 0x11D}
    ev = evaluate([envelope_to_raw(env), _raws(result)[1]],
                  kernel.repo, kernel.settings.master_key)
    # 字段不匹配者被排除；只剩 1 个 -> 不足门限
    assert ev.category == BELOW_THRESHOLD
    assert ev.field_mismatches  # 有具体指纹入诊断


def test_threshold_mismatch_excluded(kernel):
    result = issue(kernel, b"thr-bind", 2, 3)
    env = copy.deepcopy(result.shares[0])
    env["threshold"] = 5
    ev = evaluate([envelope_to_raw(env), _raws(result)[1]],
                  kernel.repo, kernel.settings.master_key)
    assert ev.category == BELOW_THRESHOLD
    assert ev.threshold_mismatches


def test_bad_tag_detected(kernel):
    result = issue(kernel, b"tag-check", 2, 3)
    env = copy.deepcopy(result.shares[0])
    env["tag"] = base64.b64encode(b"\x00" * 32).decode()
    ev = evaluate([envelope_to_raw(env), _raws(result)[1]],
                  kernel.repo, kernel.settings.master_key)
    assert ev.category == BELOW_THRESHOLD
    assert len(ev.bad_tag_fingerprints) == 1


def test_malformed_evidence_category(kernel):
    result = issue(kernel, b"shape", 2, 3)
    ev = evaluate(["not-json", "{}", _raws(result)[0]],
                  kernel.repo, kernel.settings.master_key)
    assert ev.category == BELOW_THRESHOLD  # 只有 1 个可用
    assert len(ev.malformed) == 2
    ev_all_bad = evaluate(["not-json", "{bad"], kernel.repo,
                          kernel.settings.master_key)
    assert ev_all_bad.category == MALFORMED_EVIDENCE


def test_bad_length_excluded(kernel):
    result = issue(kernel, b"lengthcheck", 2, 3)
    env = copy.deepcopy(result.shares[0])
    env["y"] = base64.b64encode(b"short").decode()
    # 重签标签以隔离 BAD_LENGTH 与 BAD_TAG
    from threshold_service.integrity import tag_share
    from threshold_service.models import ShareEnvelope, canonical_envelope_bytes
    draft = ShareEnvelope(
        version=env["version"], set_id=env["set_id"], x=env["x"], y=env["y"],
        threshold=env["threshold"], field=env["field"], tag="",
    )
    env["tag"] = base64.b64encode(
        tag_share(kernel.settings.master_key, canonical_envelope_bytes(draft))
    ).decode()
    ev = evaluate([envelope_to_raw(env), _raws(result)[1]],
                  kernel.repo, kernel.settings.master_key)
    assert ev.category == BELOW_THRESHOLD
    assert len(ev.bad_length_fingerprints) == 1
