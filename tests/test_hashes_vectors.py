"""哈希原语测试：标准向量 + 纯 Python 回退与 OpenSSL 交叉验证。"""
from __future__ import annotations

import hashlib
import os

import pytest

from stackvm import hashes as H
from stackvm.ripemd160_fallback import ripemd160 as py_ripemd160

# 标准发布测试向量
RIPEMD160_VECTORS = {
    b"": "9c1185a5c5e9fc54612808977ee8f548b2258d31",
    b"a": "0bdc9d2d256b3ee9daae347be6f4dc835a467ffe",
    b"abc": "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc",
    b"message digest": "5d0689ef49d2fae572b881b123a85ffa21595f36",
    b"abcdefghijklmnopqrstuvwxyz": "f71c27109c692c1b56bbdceb5b9d2865b3708dbc",
}
SHA_VECTORS = {
    b"abc": {
        "sha1": "a9993e364706816aba3e25717850c26c9cd0d89d",
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    },
}
# HASH160/HASH256 的零向量（Bitcoin 常见参考值）
HASH160_EMPTY = "b472a266d0bd89c13706a4132ccfb16f7c3b9fcb"
HASH256_EMPTY = "5df6e0e2761359d30a8275058e299fcc0381534545f55cf43e41983f5d4c9456"


@pytest.mark.parametrize("msg,digest", RIPEMD160_VECTORS.items())
def test_ripemd160_standard_vectors(msg, digest):
    assert H.ripemd160(msg).hex() == digest
    assert py_ripemd160(msg).hex() == digest


@pytest.mark.parametrize("msg,v", SHA_VECTORS.items())
def test_sha_standard_vectors(msg, v):
    assert H.sha1(msg).hex() == v["sha1"]
    assert H.sha256(msg).hex() == v["sha256"]


def test_hash160_hash256_empty_vectors():
    assert H.hash160(b"").hex() == HASH160_EMPTY
    assert H.hash256(b"").hex() == HASH256_EMPTY


def test_ripemd160_fallback_matches_openssl_randomized():
    """回退实现必须与 OpenSSL 在多组输入（含跨块边界长度）上逐字节一致。"""
    if "ripemd160" not in hashlib.algorithms_available:
        pytest.skip("本机 OpenSSL 无 ripemd160，仅验证标准向量")
    rng = os.urandom
    for n in list(range(0, 140)) + [255, 256, 1000]:
        msg = rng(n)
        ssl_digest = hashlib.new("ripemd160", msg).digest()
        assert py_ripemd160(msg) == ssl_digest, f"长度 {n} 输入不一致"
        assert H.ripemd160(msg) == ssl_digest


def test_composite_hashes_against_manual_composition():
    msg = b"stackvm-compound-test"
    assert H.hash160(msg) == H.ripemd160(H.sha256(msg))
    assert H.hash256(msg) == H.sha256(H.sha256(msg))
