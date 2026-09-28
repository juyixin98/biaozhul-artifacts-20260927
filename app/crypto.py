"""Authenticated encryption for audit originals.

Originals never leave the process in plaintext and are never written to the
SQLite file unencrypted. We use Fernet (AES-128-CBC + HMAC-SHA256, timestamped)
so ciphertext tampering is detected on decrypt. Each encrypted fragment is
stored alongside its plaintext SHA-256 so an auditor can prove the ciphertext
corresponds to a claimed value without revealing it.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    sha256: str


class FragmentCipher:
    def __init__(self, fernet_key: str | bytes) -> None:
        self._fernet = Fernet(
            fernet_key.encode() if isinstance(fernet_key, str) else fernet_key
        )

    def seal(self, plaintext: str) -> Sealed:
        data = plaintext.encode("utf-8")
        return Sealed(
            ciphertext=self._fernet.encrypt(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def open(self, sealed: Sealed) -> str:
        data = self._fernet.decrypt(sealed.ciphertext)
        digest = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(digest, sealed.sha256):
            raise IntegrityError("decrypted fragment does not match stored digest")
        return data.decode("utf-8")

    def open_if_matches(self, ciphertext: bytes, expected_sha256: str) -> str:
        try:
            data = self._fernet.decrypt(ciphertext)
        except InvalidToken as exc:
            raise IntegrityError("ciphertext failed authentication") from exc
        if not hmac.compare_digest(
            hashlib.sha256(data).hexdigest(), expected_sha256
        ):
            raise IntegrityError("decrypted fragment does not match stored digest")
        return data.decode("utf-8")


class IntegrityError(Exception):
    """Category ``audit_integrity_error`` — stored data was tampered with."""


def chain_hash(prev_hex: str, payload: bytes) -> str:
    """Append-only tamper-evidence link for the audit event log."""
    h = hashlib.sha256()
    h.update(prev_hex.encode("ascii"))
    h.update(b":")
    h.update(payload)
    return h.hexdigest()
