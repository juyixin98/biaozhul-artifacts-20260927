"""独立完整性层测试：标签绑定集合身份/门限/字段，承诺检测错误恢复。"""
from __future__ import annotations

import pytest

from threshold_service.integrity import (
    secret_commitment,
    tag_share,
    verify_commitment,
    verify_share_tag,
)
from threshold_service.models import (
    ENVELOPE_VERSION,
    MalformedEnvelope,
    ShareEnvelope,
    b64e,
    canonical_envelope_bytes,
    envelope_fingerprint,
)

KEY = b"k" * 32
OTHER_KEY = b"j" * 32


def _env(**over):
    base = dict(
        version=ENVELOPE_VERSION, set_id="set-abc", x=7, y=b64e(b"abc"),
        threshold=3, field={"bits": 8, "generator": 283}, tag="",
    )
    base.update(over)
    return ShareEnvelope(**base)


def test_tag_roundtrip_and_wrong_key_fails():
    env = _env()
    tag = tag_share(KEY, canonical_envelope_bytes(env))
    assert verify_share_tag(KEY, canonical_envelope_bytes(env), tag)
    assert not verify_share_tag(OTHER_KEY, canonical_envelope_bytes(env), tag)


@pytest.mark.parametrize("mutation", ["set_id", "x", "y", "threshold", "field"])
def test_tag_covers_each_bound_identity_field(mutation):
    """篡改任一绑定字段都必须使标签失效。"""
    env = _env()
    tag = tag_share(KEY, canonical_envelope_bytes(env))
    mutated = {
        "set_id": {"set_id": "set-OTHER"},
        "x": {"x": 8},
        "y": {"y": b64e(b"abd")},
        "threshold": {"threshold": 2},
        "field": {"field": {"bits": 8, "generator": 285}},
    }[mutation]
    env2 = _env(**mutated)
    assert not verify_share_tag(KEY, canonical_envelope_bytes(env2), tag)


def test_fingerprint_stable_and_sensitive():
    env = _env(tag=b64e(tag_share(KEY, canonical_envelope_bytes(_env()))))
    assert envelope_fingerprint(env) == envelope_fingerprint(env)
    other = ShareEnvelope(**{**env.to_dict(), "x": 9})
    assert envelope_fingerprint(other) != envelope_fingerprint(env)
    # 指纹不得泄露份额内容
    assert b"abc".hex() not in envelope_fingerprint(env)


def test_envelope_parsing_rejects_bad_shapes():
    good = _env(tag=b64e(b"\x00" * 32)).to_dict()
    with pytest.raises(MalformedEnvelope):
        ShareEnvelope.from_dict({})
    with pytest.raises(MalformedEnvelope):
        ShareEnvelope.from_dict({**good, "version": 9})
    with pytest.raises(MalformedEnvelope):
        ShareEnvelope.from_dict({**good, "x": 0})
    with pytest.raises(MalformedEnvelope):
        ShareEnvelope.from_dict({**good, "x": 256})
    with pytest.raises(MalformedEnvelope):
        ShareEnvelope.from_dict({**good, "threshold": 1})
    with pytest.raises(MalformedEnvelope):
        ShareEnvelope.from_dict({**good, "y": "@@@not base64@@@"})
    with pytest.raises(MalformedEnvelope):
        ShareEnvelope.from_dict({**good, "field": {"bits": 8}})
    with pytest.raises(MalformedEnvelope):
        ShareEnvelope.from_dict({**good, "set_id": ""})
    # 布尔不得冒充整数
    with pytest.raises(MalformedEnvelope):
        ShareEnvelope.from_dict({**good, "x": True})


def test_commitment_detects_wrong_secret():
    commit = secret_commitment(KEY, "set-abc", b"real secret")
    assert verify_commitment(KEY, "set-abc", b"real secret", commit)
    assert not verify_commitment(KEY, "set-abc", b"fake secret", commit)
    # 承诺绑定集合身份
    assert not verify_commitment(KEY, "set-OTHER", b"real secret", commit)
