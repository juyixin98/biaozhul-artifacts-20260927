"""哈希原语：RIPEMD160 / SHA1 / SHA256 / HASH160 / HASH256。

优先使用 OpenSSL 提供的 hashlib.ripemd160；若运行时 OpenSSL 裁剪了该算法，
退回到本仓库内置的纯 Python RIPEMD-160（ripemd160_fallback），保证可复现。
所有变体均附带标准测试向量（见 tests/test_hashes_vectors.py）。
"""
from __future__ import annotations

import hashlib

from .ripemd160_fallback import ripemd160 as _ripemd160_py


def ripemd160(data: bytes) -> bytes:
    try:
        h = hashlib.new("ripemd160")
        h.update(data)
        return h.digest()
    except (ValueError, Exception):  # noqa: BLE001 - 算法不可用时统一回退
        return _ripemd160_py(data)


def sha1(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hash160(data: bytes) -> bytes:
    return ripemd160(sha256(data))


def hash256(data: bytes) -> bytes:
    return sha256(sha256(data))


HASHES = {
    "ripemd160": ripemd160,
    "sha1": sha1,
    "sha256": sha256,
    "hash160": hash160,
    "hash256": hash256,
}
