"""Command line interface.

Examples:
    python -m osdiff.cli diff old.json new.json
    python -m osdiff.cli diff old.json new.json --json --limit 3
    python -m osdiff.cli verify policy.json request.json --expect ALLOW
    python -m osdiff.cli serve --host 127.0.0.1 --port 8080
    python -m osdiff.cli audit-verify
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .service import Service, ServiceError


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print_diff(result: Any, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result.summary(), indent=2, ensure_ascii=False))
        return 0 if not result.expands else 2
    s = result.summary()
    print(f"run_id:            {result.run_id}")
    print(f"old/new versions:  {result.old_version_id} -> {result.new_version_id}")
    print(f"request space:     {result.space_size} (bounded, exhaustively enumerated)")
    print(f"expands proven:    {result.expands}")
    print(f"possibly expands:  {result.possibly_expands}  (UNKNOWN is not access)")
    print(f"contracts:         {result.contracts}")
    print("transition counts:")
    for cat, n in s["counts"].items():
        print(f"  {cat:24s} {n}")
    if result.witnesses_truncated:
        print(f"note: witness list truncated to {result.witness_limit} per category; counts are complete")
    print("witnesses:")
    for w in result.witnesses:
        req = w.request
        print(f"  [{w.category.value}] {w.old_verdict.value} -> {w.new_verdict.value}")
        print(f"     principal={req['principal']!r} action={req['action']!r} resource={req['resource']!r}")
        attrs = req.get("attributes")
        if attrs:
            print(f"     attributes={json.dumps(attrs, ensure_ascii=False, sort_keys=True)}")
    return 0 if not result.expands else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="osdiff", description="offline object-storage policy diff")
    parser.add_argument("--config", default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--key", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("diff", help="exhaustively diff two policy documents")
    d.add_argument("old_policy")
    d.add_argument("new_policy")
    d.add_argument("--json", action="store_true")
    d.add_argument("--limit", type=int, default=None)
    d.add_argument("--cap", type=int, default=None)

    v = sub.add_parser("verify", help="re-evaluate one concrete request under one policy")
    v.add_argument("policy")
    v.add_argument("request")
    v.add_argument("--expect", choices=[x.value for x in __import__("osdiff.types", fromlist=["Verdict"]).Verdict])

    s = sub.add_parser("serve", help="run the FastAPI audit interface")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)

    sub.add_parser("audit-verify", help="verify the signed audit hash chain")
    sub.add_parser("list-runs", help="list stored runs")

    args = parser.parse_args(argv)
    cfg = load_config(
        args.config,
        data_dir=args.data_dir,
        db_path=args.db,
        key_path=args.key,
        space_cap=getattr(args, "cap", None),
        witness_limit_per_category=getattr(args, "limit", None),
    )

    try:
        with Service(cfg) as svc:
            if args.cmd == "diff":
                result = svc.run_diff(_load(args.old_policy), _load(args.new_policy))
                return _print_diff(result, args.json)
            if args.cmd == "verify":
                out = svc.verify_request(_load(args.policy), _load(args.request),
                                         expected_verdict=args.expect)
                print(json.dumps(out, indent=2, ensure_ascii=False))
                return 0 if out["consistent"] else 3
            if args.cmd == "audit-verify":
                print(json.dumps(svc.verify_audit_chain(), indent=2))
                return 0
            if args.cmd == "list-runs":
                print(json.dumps(svc.list_runs(), indent=2))
                return 0
            if args.cmd == "serve":
                import uvicorn
                uvicorn.run(__import__("osdiff.api", fromlist=["create_app"]).create_app(svc),
                            host=args.host, port=args.port)
                return 0
    except ServiceError as e:
        print(json.dumps({"ok": False, "failure_code": e.code.value,
                          "message": e.message, "details": e.details}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
