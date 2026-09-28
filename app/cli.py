"""命令行工具：

* ``gen-key``                 —— 生成 Fernet 密钥
* ``verify-audit [--path P]`` —— 校验审计哈希链
* ``fingerprint FILE``        —— 计算请求 JSON 的输入指纹
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.config import load_settings
from app.security.audit import verify_chain
from app.security.crypto import derive_signing_key, fingerprint, generate_key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anon-ctl")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("gen-key", help="generate a Fernet key")

    va = sub.add_parser("verify-audit", help="verify audit log hash chain")
    va.add_argument("--path", default=None)
    va.add_argument("--key", default=None, help="master/audit key (env ANON_ENCRYPTION_KEY)")

    fp = sub.add_parser("fingerprint", help="fingerprint a request JSON file")
    fp.add_argument("path")

    args = parser.parse_args(argv)

    if args.cmd == "gen-key":
        print(generate_key().decode())
        return 0

    if args.cmd == "verify-audit":
        import os

        settings = load_settings()
        path = Path(args.path or settings.audit_log_path)
        key_raw = args.key or os.environ.get("ANON_ENCRYPTION_KEY", "")
        if not key_raw:
            print("ERROR: provide --key or ANON_ENCRYPTION_KEY", file=sys.stderr)
            return 2
        result = verify_chain(path, derive_signing_key(key_raw.encode()))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["ok"] else 1

    if args.cmd == "fingerprint":
        data = json.loads(Path(args.path).read_text(encoding="utf-8"))
        print(fingerprint(data))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
