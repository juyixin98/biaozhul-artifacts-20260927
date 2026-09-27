"""Command-line interface.

Commands::

    searchdsl reindex   [--config config.json]
    searchdsl parse     "<query>"                # canonical tree / errors
    searchdsl search    "<query>" [--limit N] [--offset N] [--explain]
    searchdsl diagnose  "<query>" --out runs/   # JSONL run log
    searchdsl serve     [--host H] [--port P]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from searchdsl.config import load_config
from searchdsl.diagnostics import RunContext, env_run_id
from searchdsl.errors import SearchDSLError
from searchdsl.search import SearchEngine


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _add_config(p: argparse.ArgumentParser):
    p.add_argument("--config", default=None, help="path to config JSON")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="searchdsl", description="Search DSL command line")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_reindex = sub.add_parser("reindex", help="(re)build the SQLite index from fixtures")
    _add_config(p_reindex)

    p_parse = sub.add_parser("parse", help="print the canonical query tree")
    p_parse.add_argument("query")
    p_parse.add_argument("--no-validate", action="store_true")
    _add_config(p_parse)

    p_search = sub.add_parser("search", help="run a query and print matches")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--offset", type=int, default=0)
    p_search.add_argument("--explain", action="store_true")
    p_search.add_argument("--no-save", action="store_true")
    _add_config(p_search)

    p_diag = sub.add_parser("diagnose", help="run with a full JSONL diagnostic log")
    p_diag.add_argument("query")
    p_diag.add_argument("--out", default="runs", help="output directory")
    _add_config(p_diag)

    p_serve = sub.add_parser("serve", help="start the HTTP service")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    _add_config(p_serve)

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.cmd == "reindex":
        with SearchEngine(cfg) as engine:
            _print_json(engine.version.as_dict())
        return 0

    if args.cmd == "serve":
        import uvicorn

        host = args.host or cfg.server.host
        port = args.port or cfg.server.port
        uvicorn.run("searchdsl.service:app", host=host, port=port)
        return 0

    with SearchEngine(cfg) as engine:
        if args.cmd == "parse":
            from searchdsl.astnodes import canonical_hash, canonical_json
            from searchdsl.normalize import normalize
            from searchdsl.parser import parse
            from searchdsl.validate import validate

            ctx = RunContext(query=args.query, run_id=env_run_id(),
                             versions=engine.version.as_dict())
            try:
                tree = parse(args.query)
                report = None
                if not args.no_validate:
                    report = validate(tree, engine.schema, cfg.limits).as_dict()
                canonical = normalize(tree)
                out = {
                    "status": "ok",
                    "query": args.query,
                    "canonical": canonical.to_canonical(),
                    "canonical_json": canonical_json(canonical),
                    "query_hash": canonical_hash(canonical),
                    "budget": report,
                    "versions": engine.version.as_dict(),
                    "run_id": ctx.run_id,
                }
                _print_json(out)
                return 0
            except SearchDSLError as exc:
                _print_json({"status": "error", "query": args.query,
                             "error": exc.as_dict(), "run_id": ctx.run_id})
                return 2

        if args.cmd == "search":
            resp = engine.search(
                args.query,
                limit=args.limit,
                offset=args.offset,
                explain=args.explain,
                save=not args.no_save,
            )
            _print_json(resp.as_dict())
            return 0 if resp.status == "ok" else 2

        if args.cmd == "diagnose":
            resp = engine.search(args.query, explain=True)
            out_dir = Path(args.out)
            log_path = out_dir / f"{resp.run_id}.jsonl"
            ctx_dict = resp.diagnostics
            log_path.parent.mkdir(parents=True, exist_ok=True)
            lines = [json.dumps({"type": "summary", **ctx_dict["summary"]},
                                ensure_ascii=False, sort_keys=True)]
            lines.extend(
                json.dumps(e, ensure_ascii=False, sort_keys=True)
                for e in ctx_dict["events"]
            )
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            _print_json({"status": resp.status, "run_id": resp.run_id,
                         "log": str(log_path), "error": resp.error,
                         "total": resp.total})
            return 0 if resp.status == "ok" else 2

    return 1  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
