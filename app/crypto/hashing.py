"""Hashing primitives for the security kernel.

SHA-256 (via the audited ``cryptography`` package) is the only hash used.
Randomness comes from ``os.urandom``. No MD5/SHA-1, no custom mixing.
"""
from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes


def sha256(data: bytes) -> bytes:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(data)
    return digest.finalize()


def random_salt(n: int) -> bytes:
    if n < 16:
        # Refuse to run with salt entropy below 128 bits even if misconfigured.
        raise ValueError(f"refusing to generate salt shorter than 16 bytes, got {n}")
    return os.urandom(n)


EMPTY_SHA256 = sha256(b"")
