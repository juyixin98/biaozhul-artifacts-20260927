"""Symmetric encryption helpers for the audit store (cryptography/Fernet).

The audit record contains request metadata and *redacted* evidence; even so it
is encrypted at rest with a locally managed key. A separate HMAC key derives a
lookup token from the request id, so the plaintext request id never appears in
a database column.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes


def _derive(secret: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=length, salt=b"sqlguard-v1",
        info=info,
    ).derive(secret)


class KeyMaterial:
    """Holds the master secret and derives per-purpose keys deterministically."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 16:
            raise ValueError("master secret must be at least 16 bytes")
        self._secret = secret
        fer = base64.urlsafe_b64encode(_derive(secret, b"fernet", 32))
        self.fernet = Fernet(fer)
        self.hmac_key = _derive(secret, b"hmac-index", 32)

    @classmethod
    def generate(cls) -> "KeyMaterial":
        return cls(os.urandom(32))

    @classmethod
    def load_or_create(cls, key_path: str | Path) -> "KeyMaterial":
        p = Path(key_path)
        if p.exists():
            secret = p.read_bytes()
        else:
            secret = os.urandom(32)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(secret)
            try:
                p.chmod(0o600)
            except OSError:
                pass
        return cls(secret)

    def index_token(self, request_id: str) -> str:
        return hmac.new(
            self.hmac_key, request_id.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def encrypt(self, plaintext: bytes) -> bytes:
        return self.fernet.encrypt(plaintext)

    def decrypt(self, token: bytes) -> bytes:
        return self.fernet.decrypt(token)
