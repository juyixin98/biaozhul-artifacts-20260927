"""签名核验：缺签、未知公钥、篡改载荷各自的失败类别。"""

from __future__ import annotations

import json

import pytest

from diffanalyzer.crypto_verify import KeyRegistry, sign
from diffanalyzer.crypto_verify import generate_private_key
from diffanalyzer.models import FailureKind


def _registry(pub_pem):
    return KeyRegistry.from_pems({"submitter": pub_pem.decode()})


def test_valid_signature_passes(keys):
    reg = _registry(keys["pub_pem"])
    from diffanalyzer.crypto_verify import load_private_key
    payload = {"a": 1, "b": [1, 2]}
    sig = sign(load_private_key(keys["priv_pem"]), payload)
    reg.verify("submitter", payload, sig)  # 不抛异常即通过


def test_missing_signature_is_distinct_failure(keys):
    reg = _registry(keys["pub_pem"])
    with pytest.raises(Exception) as ei:
        reg.verify("submitter", {"a": 1}, None)
    assert ei.value.kind is FailureKind.CRYPTO_MISSING_SIGNATURE


def test_unregistered_key_is_distinct_failure(keys):
    reg = _registry(keys["pub_pem"])
    from diffanalyzer.crypto_verify import load_private_key
    sig = sign(load_private_key(keys["other_priv_pem"]), {"a": 1})
    with pytest.raises(Exception) as ei:
        reg.verify("mallory", {"a": 1}, sig)
    assert ei.value.kind is FailureKind.CRYPTO_UNREGISTERED_KEY
    assert "mallory" in ei.value.details["submitter"]


def test_tampered_payload_fails_signature(keys):
    reg = _registry(keys["pub_pem"])
    from diffanalyzer.crypto_verify import load_private_key
    key = load_private_key(keys["priv_pem"])
    payload = {"version": "v1", "rules": []}
    sig = sign(key, payload)
    tampered = dict(payload, version="v2")
    with pytest.raises(Exception) as ei:
        reg.verify("submitter", tampered, sig)
    assert ei.value.kind is FailureKind.CRYPTO_BAD_SIGNATURE


def test_wrong_submitter_name_fails_even_with_same_key(keys):
    # 签名正确但提交者身份与注册名不符（用另一注册位）必须拒绝
    reg = KeyRegistry.from_pems({
        "submitter": keys["pub_pem"].decode(),
        "other": keys["other_pub_pem"].decode(),
    })
    from diffanalyzer.crypto_verify import load_private_key
    sig = sign(load_private_key(keys["priv_pem"]), {"x": 1})
    with pytest.raises(Exception) as ei:
        reg.verify("other", {"x": 1}, sig)
    assert ei.value.kind is FailureKind.CRYPTO_BAD_SIGNATURE
