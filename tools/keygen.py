"""生成新的确定性测试密钥（可选；默认夹具已由 make_fixtures 用固定种子生成）。

    python -m tools.keygen alice extra-key-1
输出 SEC1 压缩公钥与 32 字节标量私钥（hex）。仅用于本地测试。
"""
from __future__ import annotations

import hashlib
import sys

import ecdsa
from ecdsa import SECP256k1, SigningKey


def derive(name: str, label: str) -> SigningKey:
    seed = f"stackvm-keygen::{label}::{name}::v1".encode()
    return SigningKey.from_string(hashlib.sha256(seed).digest(), curve=SECP256k1)


def main(argv: list[str]) -> int:
    if not argv:
        print("用法: python -m tools.keygen <名字> [<标签>]", file=sys.stderr)
        return 1
    name, label = argv[0], (argv[1] if len(argv) > 1 else "adhoc")
    sk = derive(name, label)
    pt = sk.get_verifying_key().pubkey.point
    prefix = b"\x02" if pt.y() % 2 == 0 else b"\x03"
    pub = prefix + pt.x().to_bytes(32, "big")
    print(f"name   = {name}")
    print(f"priv   = {sk.to_string().hex()}  (TEST ONLY, deterministic seed)")
    print(f"pub    = {pub.hex()}")
    print(f"curve  = secp256k1 (ecdsa lib, NIST SEC1 compressed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
