"""Content fingerprints.

Candidate fingerprints bind a finding to the *content* of the matched value,
not to a file name or line number, so moving or renaming a file does not
discharge or duplicate the finding.

The HMAC key is derived per-project (HKDF-SHA256 over a random project master
key with a per-project random salt): identical content in different projects
produces unrelated fingerprints, and an attacker who learns a fingerprint
cannot mount an offline dictionary attack without the project key.
"""

from __future__ import annotations

import hashlib
import hmac

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

HKDF_INFO = b"secretscan-candidate-fingerprint-v1"
KEY_LENGTH = 32
SALT_LENGTH = 16
MASTER_LENGTH = 32


class CandidateFingerprinter:
    def __init__(self, master_key: bytes, project_salt: bytes):
        if len(master_key) != MASTER_LENGTH:
            raise ValueError("master_key must be 32 bytes")
        if len(project_salt) != SALT_LENGTH:
            raise ValueError("project_salt must be 16 bytes")
        self._key = HKDF(
            algorithm=hashes.SHA256(),
            length=KEY_LENGTH,
            salt=project_salt,
            info=HKDF_INFO,
        ).derive(master_key)

    def fingerprint(self, raw: bytes) -> str:
        return hmac.new(self._key, raw, hashlib.sha256).hexdigest()


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_digest(data: bytes) -> str:
    """Stable digest of configuration content (binds scan to exact config)."""
    return hashlib.sha256(data).hexdigest()
