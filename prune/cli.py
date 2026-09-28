"""Command line: catalog refresh and API server.

Usage:
  python -m prune.cli --config config/prune.toml refresh [--table NAME]
  python -m prune.cli --config config/prune.toml serve --host 127.0.0.1 --port 8000
  python -m scripts.make_fixtures ...    (see scripts/)
"""

from __future__ import annotations

import argparse

from .catalog import Catalog
from .config import load_config
from .logctx import configure_logging
from .transforms import tzdb_version


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="prune")
    ap.add_argument("--config", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("refresh").add_argument("--table")
    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    log = configure_logging()
    config = load_config(args.config)

    if args.cmd == "refresh":
        targets = [args.table] if args.table else sorted(config.tables)
        for name in targets:
            spec = config.tables.get(name)
            if spec is None:
                raise SystemExit(f"unknown table {name!r}")
            with Catalog(config.catalog_db) as cat:
                summary = cat.refresh_table(spec)
            log.info("refreshed table", extra={
                "table": name, "stage": "refresh",
                "detail": {"tzdb": tzdb_version(), **summary}})
            print(f"refreshed {name}: {summary}")
        return 0

    if args.cmd == "serve":
        import uvicorn
        from .api import create_app
        app = create_app(config)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return 0

    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
