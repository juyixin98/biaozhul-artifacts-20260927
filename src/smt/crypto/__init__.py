"""Cryptographic building blocks: encoding spec, hashing, record signing."""
from .encoding import (
    HASH_BYTES,
    KEY_BITS,
    KEY_BYTES,
    SPEC_VERSION,
    bit_at,
    canonical_json,
    encode_branch,
    encode_leaf,
    normalize_key,
    normalize_value,
)
from .hashing import empty_at, empty_hashes, hash_branch, hash_leaf, sha256, sign_payload, verify_payload

__all__ = [
    "HASH_BYTES",
    "KEY_BITS",
    "KEY_BYTES",
    "SPEC_VERSION",
    "bit_at",
    "canonical_json",
    "encode_branch",
    "encode_leaf",
    "normalize_key",
    "normalize_value",
    "empty_at",
    "empty_hashes",
    "hash_branch",
    "hash_leaf",
    "sha256",
    "sign_payload",
    "verify_payload",
]
