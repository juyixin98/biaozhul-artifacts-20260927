"""Crypto + audit-store tests.

Verifies encryption round-trips, that the request id is not stored in plain
text, and that the redacted review body is what gets encrypted.
"""

from __future__ import annotations

import json

import pytest

from sqlguard.audit_store import AuditStore
from sqlguard.crypto import KeyMaterial


@pytest.fixture()
def key():
    return KeyMaterial.generate()


@pytest.fixture()
def store(tmp_path, key):
    s = AuditStore(tmp_path / "audit.db", key)
    yield s
    s.close()


def test_keyfile_is_created_with_restrictive_perms(tmp_path):
    kp = tmp_path / "k.key"
    KeyMaterial.load_or_create(kp)
    assert kp.exists()
    mode = kp.stat().st_mode & 0o777
    assert mode == 0o600


def test_fernet_round_trip(key):
    token = key.encrypt(b"secret-body")
    assert token != b"secret-body"
    assert key.decrypt(token) == b"secret-body"


def test_index_token_is_keyed_and_stable(key):
    a = key.index_token("rev_1")
    b = key.index_token("rev_1")
    c = key.index_token("rev_2")
    assert a == b and a != c
    assert "rev_1" not in a  # HMAC, not reversible


def test_record_persists_encrypted_and_fetches_by_request_id(store):
    payload = {
        "request_id": "rev_abc", "verdict": "accept",
        "findings": [], "sql_digest": "d1",
    }
    store.record(payload)
    fetched = store.fetch("rev_abc")
    assert fetched["verdict"] == "accept"

    # ciphertext on disk must not contain the plaintext request id
    raw = (store.path and __import__("pathlib").Path(store.path)).read_bytes()
    assert b"rev_abc" not in raw


def test_fetch_missing_returns_none(store):
    assert store.fetch("rev_does_not_exist") is None


def test_recent_verdicts_returns_redacted_metadata(store):
    for i in range(3):
        store.record({"request_id": f"rev_{i}", "verdict": "reject",
                      "findings": [], "sql_digest": "x"})
    rows = store.recent_verdicts()
    assert len(rows) == 3
    assert all("ciphertext" not in r for r in rows)
    assert all(set(r) == {"record_index", "verdict", "created_at"} for r in rows)


def test_audit_body_never_contains_raw_secret(store, kernel):
    result = kernel.review(
        "UPDATE users SET email = :secret WHERE id = :id",
        parameters={"secret": "TOPSECRET123", "id": 1},
    )
    store.record(result.to_dict())
    fetched = store.fetch(result.request_id)
    assert "TOPSECRET123" not in json.dumps(fetched)
