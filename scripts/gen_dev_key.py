#!/usr/bin/env python3
"""生成本地合成开发用 Ed25519 密钥对（绝非生产密钥）。

私钥写 configs/dev_signing_key.pem（0600），公钥写 .pem.pub 供离线核验演示。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.coding.signing import (  # noqa: E402
    generate_private_key,
    private_key_to_pem,
    public_key_to_pem,
)


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "configs/dev_signing_key.pem")
    out.parent.mkdir(parents=True, exist_ok=True)
    key = generate_private_key()
    out.write_bytes(private_key_to_pem(key))
    out.chmod(0o600)
    pub_path = out.with_suffix(out.suffix + ".pub")
    pub_path.write_bytes(public_key_to_pem(key.public_key()))
    print(f"wrote private key: {out}")
    print(f"wrote public key:  {pub_path}")
    print("REMINDER: 合成开发密钥，勿用于生产；私钥已被 .gitignore 忽略")


if __name__ == "__main__":
    main()
