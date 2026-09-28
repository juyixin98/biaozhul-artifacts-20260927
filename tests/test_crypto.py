"""静态加密：密文落盘、错钥解密失败、密钥来源区分、无钥拒绝启动。"""

from __future__ import annotations

import pytest

from app.core.errors import FailureCode, ServiceError
from app.security.crypto import (
    CryptoBox,
    build_crypto_box,
    fingerprint,
    generate_key,
)
from cryptography.fernet import Fernet


def test_ciphertext_at_rest_and_roundtrip(state, tmp_path):
    from tests.conftest import load_fixture, make_payload

    fx = load_fixture("tiny_patients")
    state.service.submit_dataset(make_payload(fx, 2, 2))

    # 数据库文件中不应出现明文敏感值
    raw = (tmp_path / "state" / "test.db").read_bytes()
    for secret in (b"flu", b"diabetes", b"Haidian"):
        assert secret not in raw


def test_wrong_key_fails_to_decrypt(state):
    token = state.service.crypto.encrypt_json({"a": 1})
    other = CryptoBox(Fernet(generate_key()), "configured")
    with pytest.raises(ServiceError) as ei:
        other.decrypt_json(token)
    assert ei.value.code is FailureCode.DECRYPTION_FAILED


def test_fingerprint_stable_and_short():
    a = fingerprint({"x": 1, "y": [1, 2]})
    b = fingerprint({"y": [1, 2], "x": 1})  # 键序无关
    c = fingerprint({"x": 2, "y": [1, 2]})
    assert a == b and a != c
    assert len(a) == 32


def test_ephemeral_vs_configured_key_source():
    box = build_crypto_box("", allow_ephemeral=True)
    assert box.key_source == "ephemeral"

    key = generate_key().decode()
    box2 = build_crypto_box(key, allow_ephemeral=False)
    assert box2.key_source == "configured"


def test_missing_key_without_ephemeral_raises():
    with pytest.raises(ServiceError) as ei:
        build_crypto_box("", allow_ephemeral=False)
    assert ei.value.code is FailureCode.INVALID_INPUT


def test_invalid_key_rejected():
    with pytest.raises(ServiceError) as ei:
        build_crypto_box("not-a-fernet-key", allow_ephemeral=False)
    assert ei.value.code is FailureCode.INVALID_INPUT


def test_ephemeral_key_cannot_decrypt_prior_ciphertext(tmp_path):
    """进程重启（新临时密钥）后旧密文不可解 —— 状态隔离语义。"""
    from app.config import Settings
    from app.state import build_state

    s1 = Settings(
        database_path=str(tmp_path / "a.db"),
        audit_log_path=str(tmp_path / "a.jsonl"),
        encryption_key="",
        allow_ephemeral_key=True,
    )
    st1 = build_state(s1)
    token = st1.service.crypto.encrypt_json({"secret": "x"})

    s2 = Settings(
        database_path=str(tmp_path / "b.db"),
        audit_log_path=str(tmp_path / "b.jsonl"),
        encryption_key="",
        allow_ephemeral_key=True,
    )
    from app.state import build_state

    st2 = build_state(s2)
    with pytest.raises(ServiceError) as ei:
        st2.service.crypto.decrypt_json(token)
    assert ei.value.code is FailureCode.DECRYPTION_FAILED
