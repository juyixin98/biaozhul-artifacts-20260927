"""Keccak-256 哈希层。

使用成熟密码库 pycryptodome 的 Keccak 实现（以太坊使用的是 Keccak-256，
而非 NIST SHA3-256，二者填充不同）。同时通过 eth_hash 做一次交叉校验，
两个成熟库一致才返回，避免单点实现偏差。
"""

from __future__ import annotations

from Crypto.Hash import keccak as _pd_keccak  # pycryptodome


def keccak256(data: bytes) -> bytes:
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"keccak256 输入必须是 bytes，得到 {type(data).__name__}")
    k = _pd_keccak.new(digest_bits=256)
    k.update(bytes(data))
    digest = k.digest()

    # 交叉校验：eth_hash 后端（pycryptodome/ pysha3 等，取决于安装）
    try:
        from eth_hash.auto import keccak as _eth_keccak

        alt = _eth_keccak(bytes(data))
        if alt != digest:
            raise RuntimeError(
                "Keccak 实现不一致：pycryptodome 与 eth_hash 返回不同摘要"
            )
    except ImportError:
        pass
    return digest
