"""共享测试夹具：独立临时密钥 + 临时 SQLite，每个测试函数级隔离。"""

from __future__ import annotations

import pytest

from diffanalyzer.audit import Auditor
from diffanalyzer.config import Config
from diffanalyzer.crypto_verify import (
    generate_private_key,
    private_pem,
    public_pem,
    KeyRegistry,
)
from diffanalyzer.local_signing import signed_evidence_envelope, signed_policy_envelope
from diffanalyzer.service import PolicyService
from diffanalyzer.store import Store


@pytest.fixture
def keys():
    priv = generate_private_key()
    other = generate_private_key()
    return {
        "priv_pem": private_pem(priv),
        "pub_pem": public_pem(priv),
        "other_priv_pem": private_pem(other),
        "other_pub_pem": public_pem(other),
    }


@pytest.fixture
def sign_policy(keys):
    def _sign(doc, submitted_by="submitter"):
        return signed_policy_envelope(keys["priv_pem"], doc,
                                      submitted_by=submitted_by)
    return _sign


@pytest.fixture
def sign_evidence(keys):
    def _sign(**kwargs):
        kwargs.setdefault("submitted_by", "submitter")
        return signed_evidence_envelope(keys["priv_pem"], **kwargs)
    return _sign


@pytest.fixture
def service(tmp_path, keys):
    cfg = Config(
        root=tmp_path,
        sqlite_path=str(tmp_path / "test.db"),
        resource_alphabet=["0", "1", "/", "-", "a", "b"],
        principals=["acct/alice", "acct/bob"],
        include_anonymous=True,
        max_space_size=500_000,
        trusted_key_path="",
        demo_private_key_path="",
        api_host="127.0.0.1",
        api_port=0,
        witness_limit_per_bucket=50,
        max_trace_steps=24,
    )
    store = Store(cfg.sqlite_path)
    auditor = Auditor(store)
    registry = KeyRegistry.from_pems({"submitter": keys["pub_pem"].decode()})
    svc = PolicyService(cfg, store, auditor, registry)
    yield svc
    store.close()


@pytest.fixture
def rid(service):
    return service.auditor.new_request_id()
