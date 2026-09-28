"""Command-line interface.

Commands:
    serve                         run the FastAPI app with uvicorn
    seed-fixtures --out DIR       write synthetic registry + scenario fixture
    demo --db PATH                in-process end-to-end demonstration
    replay --db PATH              offline replay a SQLite journal
    recheck --db PATH --id ID     independently re-verify one evidence bundle
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .logging_utils import JsonRunLogger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="localffg", description="Local double/surround vote evidence detector")
    parser.add_argument("--config", default=None, help="YAML config path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run HTTP service")

    p_seed = sub.add_parser("seed-fixtures", help="generate synthetic registry + scenario fixture")
    p_seed.add_argument("--out", required=True, help="output directory")

    p_demo = sub.add_parser("demo", help="in-process end-to-end demo")
    p_demo.add_argument("--db", default=None)

    p_replay = sub.add_parser("replay", help="offline replay of a journal db")
    p_replay.add_argument("--db", required=True)
    p_replay.add_argument("--quiet", action="store_true")

    p_check = sub.add_parser("recheck", help="independently re-check evidence")
    p_check.add_argument("--db", required=True)
    p_check.add_argument("--id", required=True, dest="evidence_id")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.cmd == "serve":
        import uvicorn

        from .api import create_app

        app = create_app(cfg)
        uvicorn.run(app, host=cfg.http_host, port=cfg.http_port, log_level=cfg.log_level.lower())
        return 0

    if args.cmd == "seed-fixtures":
        from .fixtures_builder import build_fixture_set

        out = Path(args.out)
        paths = build_fixture_set(out)
        print(json.dumps({"ok": True, "written": paths}, indent=2, sort_keys=True))
        return 0

    if args.cmd == "demo":
        from .demo import run_demo

        db = args.db or cfg.db_path
        code = run_demo(db_path=db, cfg=cfg)
        return code

    if args.cmd == "replay":
        from .replay import replay_store
        from .storage import VoteStore

        logger = JsonRunLogger(echo=not args.quiet)
        with VoteStore(args.db) as store:
            report = replay_store(store, logger=logger)
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        return 0 if report.verdict == "OK" else 2

    if args.cmd == "recheck":
        from .checker import recheck_evidence
        from .storage import VoteStore

        with VoteStore(args.db) as store:
            bundle = store.get_evidence(args.evidence_id)
            if bundle is None:
                print(json.dumps({"verdict": "invalid", "failures": ["evidence not found"]}))
                return 2
            report = recheck_evidence(bundle, store.load_registry(), domain=store.load_domain())
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        return 0 if report.verdict.value == "valid" else 2

    parser.error("unknown command")
    return 1


if __name__ == "__main__":
    sys.exit(main())
