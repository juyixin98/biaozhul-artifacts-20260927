"""Local uvicorn entrypoint: ``python -m secretscan.serve --help``.

The server binds 127.0.0.1 by default (loopback only) and requires scan roots
to be explicitly allow-listed via repeated ``--allow-root`` arguments.
"""

from __future__ import annotations

import argparse

import uvicorn

from .api import create_app
from .baseline import BaselineError, load_baseline
from .config import (ConfigError, load_rule_pack, load_scope_pack,
                     load_settings)
from .security import Fingerprinter


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="secretscan-serve",
                                description="Run the local audit HTTP API.")
    p.add_argument("--rules", default="config/rules/default.toml")
    p.add_argument("--scope", default="config/scopes/default.toml")
    p.add_argument("--workspace", default=".secretscan/workspace.db")
    p.add_argument("--baseline", default=None)
    p.add_argument("--allow-root", action="append", default=[],
                   help="directory scans are restricted to (repeatable)")
    p.add_argument("--log-file", default=None)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8099)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rules = load_rule_pack(args.rules)
        scope = load_scope_pack(args.scope)
        settings = load_settings(
            args.workspace, allowed_roots=tuple(args.allow_root),
            log_file=args.log_file)
        baseline = (load_baseline(args.baseline,
                                  Fingerprinter(settings.fingerprint_pepper))
                    if args.baseline else None)
    except (ConfigError, BaselineError) as exc:
        print(f"configuration error: {exc}")
        return 2
    app = create_app(settings, rules, scope, baseline)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
