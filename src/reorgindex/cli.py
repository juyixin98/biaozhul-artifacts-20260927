"""命令行入口：

  reorgindex generate-fixtures <dir>   生成确定性夹具与独立期望
  reorgindex replay <db> <file.jsonl>  离线回放
  reorgindex rebuild-check <db>        全量重建并与当前派生索引核对
  reorgindex serve [--db ...]          启动 HTTP 服务
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import DEFAULT_DB_PATH, Settings
from .diagnostics import JsonDiagnostics
from .fixturegen import generate_all
from .kernel import ChainKernel
from .replay import replay_jsonl, verify_against_rebuild
from .storage import Storage


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reorgindex")
    sub = parser.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("generate-fixtures", help="生成确定性合成夹具")
    gen.add_argument("out_dir")

    rep = sub.add_parser("replay", help="离线回放 JSONL 区块")
    rep.add_argument("db")
    rep.add_argument("jsonl")
    rep.add_argument("--finality-depth", type=int, default=6)

    chk = sub.add_parser("rebuild-check", help="全量重建并与当前索引核对")
    chk.add_argument("db")
    chk.add_argument("--finality-depth", type=int, default=6)

    srv = sub.add_parser("serve", help="启动 FastAPI 服务")
    srv.add_argument("--db", default=None)
    srv.add_argument("--finality-depth", type=int, default=6)
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    if args.cmd == "generate-fixtures":
        written = generate_all(args.out_dir)
        _print({"ok": True, "generated": sorted(written),
                "dir": args.out_dir})
        return 0

    if args.cmd == "replay":
        settings = Settings(db_path=args.db, finality_depth=args.finality_depth)
        storage = Storage(settings.db_path)
        diag = JsonDiagnostics(level="WARNING")
        kernel = ChainKernel(storage, settings, diag)
        report = replay_jsonl(kernel, args.jsonl)
        result = {"report": report.to_dict(), "state": kernel.state_summary(),
                  "rebuild_check": verify_against_rebuild(kernel)}
        _print(result)
        storage.close()
        return 0 if result["rebuild_check"]["ok"] else 2

    if args.cmd == "rebuild-check":
        settings = Settings(db_path=args.db, finality_depth=args.finality_depth)
        storage = Storage(settings.db_path)
        kernel = ChainKernel(storage, settings)
        result = verify_against_rebuild(kernel)
        _print({"ok": result["ok"], "mismatches": result["mismatches"],
                "current": result["current"], "rebuilt": result["rebuilt"]})
        storage.close()
        return 0 if result["ok"] else 2

    if args.cmd == "serve":  # pragma: no cover - 进程入口
        import uvicorn

        settings = Settings(
            db_path=args.db or Settings.DEFAULT_DB_PATH,
            finality_depth=args.finality_depth,
        )
        from .api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
