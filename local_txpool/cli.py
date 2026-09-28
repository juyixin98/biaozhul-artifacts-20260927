"""命令行入口：

* ``serve``           启动 FastAPI 服务（uvicorn）
* ``init-db``         初始化 SQLite schema
* ``replay-scenario`` 离线回放 YAML 夹具（含独立预言机差分）
* ``replay-audit``    从审计日志重放事件时间线（只读复核）
* ``gen-key``         生成一个本地合成 secp256k1 私钥（仅用于夹具）
"""

from __future__ import annotations

import argparse
import json
import sys

import uvicorn

from . import __version__
from .core import crypto
from .core.config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-txpool")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")

    sub.add_parser("init-db", help="初始化数据库 schema")

    p_rep = sub.add_parser(
        "replay-scenario", help="离线回放 YAML 夹具（含差分预言机）"
    )
    p_rep.add_argument("scenario", help="夹具 YAML 路径")
    p_rep.add_argument(
        "--json", action="store_true", help="输出 JSON 报告"
    )

    p_audit = sub.add_parser(
        "replay-audit", help="只读回放数据库审计事件时间线"
    )
    p_audit.add_argument("--after-id", type=int, default=0)
    p_audit.add_argument("--limit", type=int, default=1000)

    p_key = sub.add_parser("gen-key", help="生成合成私钥（仅本地夹具用）")
    p_key.add_argument("--name", help="可选：确定性派生名（keccak(name)）")

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        uvicorn.run(
            "local_txpool.api.service:asgi_factory",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
        return 0

    config = load_config()

    if args.cmd == "init-db":
        from .storage.repository import connect, init_schema

        conn = connect(config.database_path)
        init_schema(conn)
        print(f"schema initialized at {config.database_path}")
        return 0

    if args.cmd == "gen-key":
        if args.name:
            from eth_hash.auto import keccak

            raw = keccak(f"local-txpool/synthetic:{args.name}".encode())
        else:
            raw = crypto.generate_private_key()
        pk = crypto.private_key_from_hex("0x" + raw.hex())
        print("private_key:", "0x" + pk.hex())
        print("address:    ", crypto.address_for_private_key(pk))
        return 0

    if args.cmd == "replay-scenario":
        from .offline.scenario import (
            ScenarioAssertionError,
            run_scenario_file,
        )

        try:
            result = run_scenario_file(args.scenario)
        except ScenarioAssertionError as exc:
            print(f"夹具回放失败: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(
                json.dumps(
                    {
                        "scenario": result.name,
                        "steps": [
                            {"index": r.index, "kind": r.kind,
                             "detail": _jsonable(r.detail)}
                            for r in result.reports
                        ],
                        "tx_hashes": result.tx_hashes,
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
        else:
            print(f"场景 {result.name} 回放成功，共 {len(result.reports)} 步")
            for r in result.reports:
                print(f"  [{r.index:>2}] {r.kind}: {_jsonable(r.detail)}")
        return 0

    if args.cmd == "replay-audit":
        from .storage.repository import connect, init_schema

        conn = connect(config.database_path)
        init_schema(conn)
        rows = conn.execute(
            "SELECT audit_id, request_id, occurred_at_ms, event_type, "
            "tx_hash, block_hash, reason, module, service_version, detail_json "
            "FROM audit_log WHERE audit_id > ? ORDER BY audit_id LIMIT ?",
            (args.after_id, args.limit),
        ).fetchall()
        for r in rows:
            print(
                f"#{r['audit_id']:<5} {r['occurred_at_ms']} "
                f"{r['event_type']:<22} reason={r['reason']:<28} "
                f"req={r['request_id']} tx={(r['tx_hash'] or '')[:12]} "
                f"blk={(r['block_hash'] or '')[:12]} v{r['service_version']}"
            )
        print(f"-- {len(rows)} 条事件")
        return 0

    parser.print_help()
    return 1


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
