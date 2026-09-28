"""生成本地合成 Ed25519 密钥对（演示与测试夹具用；无生产账号）。"""

from __future__ import annotations

import sys
from pathlib import Path

from diffanalyzer.crypto_verify import (
    generate_private_key,
    private_pem,
    public_pem,
)


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    key = generate_private_key()
    (out / "submitter_private.pem").write_bytes(private_pem(key))
    (out / "submitter_public.pem").write_bytes(public_pem(key))
    print(f"wrote {out/'submitter_private.pem'} (demo only, never deployed)")
    print(f"wrote {out/'submitter_public.pem'} (trusted anchor)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "fixtures/keys")
