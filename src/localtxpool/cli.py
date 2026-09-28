"""命令行入口：

* ``local-txpool serve``          启动 FastAPI 服务
* ``local-txpool replay EVENTS``  离线回放事件文件，输出 trace JSON
* ``local-txpool verify EVENTS GOLDEN``
                                   回放并与人工黄金期望核对
* ``local-txpool keygen LABEL...``生成合成测试私钥/地址
"""

from __future__ import annotations

import argparse
import json
import sys

from eth_keys import keys as ek_keys

from . import __version__
from .clock import VirtualClock
from .config import Config
from .logging_setup import configure_logging
from .replay.golden import GoldenMismatch, verify
from .replay.runner import Replayer
from .service import Service


def _load_config(path: str | None) -> Config:
    return Config.load(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-txpool", description="本地账户交易池后端")
    parser.add_argument("--config", help="TOML 配置路径", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("version")

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)

    p_replay = sub.add_parser("replay", help="离线回放事件 JSONL")
    p_replay.add_argument("events")
    p_replay.add_argument("--start-time", type=int, default=0)
    p_replay.add_argument("--golden", help="可选：同时与黄金期望核对")
    p_replay.add_argument("--out", help="trace 输出路径（默认 stdout）")

    p_verify = sub.add_parser("verify", help="回放并核对黄金期望")
    p_verify.add_argument("events")
    p_verify.add_argument("golden")
    p_verify.add_argument("--start-time", type=int, default=0)

    p_keygen = sub.add_parser("keygen", help="生成合成测试私钥")
    p_keygen.add_argument("labels", nargs="+")

    args = parser.parse_args(argv)
    config = _load_config(args.config)

    if args.cmd == "version":
        print(json.dumps({"name": "local-txpool", "version": __version__}, ensure_ascii=False))
        return 0

    if args.cmd == "keygen":
        out = {}
        for label in args.labels:
            pk = ek_keys.PrivateKey(__import__("secrets").token_bytes(32))
            out[label] = {
                "private_key": "0x" + pk.to_bytes().hex(),
                "address": pk.public_key.to_checksum_address(),
            }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if args.cmd in ("replay", "verify"):
        replayer = Replayer(config, start_time=args.start_time)
        replayer.run_file(args.events)
        if args.cmd == "verify" or args.golden:
            golden_path = args.golden if args.cmd == "replay" else args.golden
            try:
                report = verify(replayer, golden_path)
            except GoldenMismatch as exc:
                print(str(exc), file=sys.stderr)
                return 2
        else:
            report = None
        payload = {
            "traces": [t.to_dict() for t in replayer.traces],
            "final_snapshot": replayer.snapshot(),
            "golden_report": report,
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        if args.cmd == "replay" and args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"trace written to {args.out}")
        else:
            print(text)
        return 0

    if args.cmd == "serve":
        import uvicorn
        from .api.app import create_app

        configure_logging(config.log.level, config.log.json)
        service = Service(config)
        app = create_app(service)
        host = args.host or config.api.host
        port = args.port or config.api.port
        uvicorn.run(app, host=host, port=port, log_config=None)
        return 0

    parser.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
