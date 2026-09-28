"""启动 FastAPI 服务（同时后台拉起仅环回的本机测试源站）。

用法::

    POLICY_FILE=fixtures/policy.json ZONE_FILE=fixtures/dns/primary.zone \
    AUDIT_DB=artifacts/audit.sqlite3 \
    python scripts/serve.py --host 127.0.0.1 --port 8080

服务本身只绑定 127.0.0.1；它访问的"外部"也只有受控 DNS 夹具与本机源站，
全程不触公网。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402

from safeproxy.service.api import create_app  # noqa: E402
from safeproxy.service.origin import OriginServer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--origin-port", type=int, default=18080)
    args = ap.parse_args()

    os.environ.setdefault("POLICY_FILE", str(ROOT / "fixtures" / "policy.json"))
    os.environ.setdefault("ZONE_FILE", str(ROOT / "fixtures" / "dns" / "primary.zone"))
    os.environ.setdefault("AUDIT_DB", str(ROOT / "artifacts" / "audit.sqlite3"))

    origin = OriginServer(args.origin_port)
    origin.__enter__()
    print(f"[origin] 本机测试源站监听 127.0.0.1:{args.origin_port}", flush=True)

    app = create_app()
    print(f"[proxy]  校验代理监听 {args.host}:{args.port}", flush=True)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        origin.__exit__(None, None, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
