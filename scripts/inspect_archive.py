#!/usr/bin/env python3
"""CLI for local offline inspection without starting the HTTP service.

Example::

    python scripts/inspect.py fixtures/sample.zip --home ./var-cli
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from archguard.audit import AuditLogger  # noqa: E402
from archguard.config import Config  # noqa: E402
from archguard.engine import Engine  # noqa: E402
from archguard.isolation import ensure_home  # noqa: E402
from archguard.store import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="inspect one archive locally")
    parser.add_argument("archive", help="path to a .zip or .tar file")
    parser.add_argument("--config", help="JSON config file")
    parser.add_argument("--home", default="./var-cli", help="service home dir")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="stream audit log to stderr"
    )
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(message)s",
            stream=sys.stderr,
        )

    cfg = Config.load(args.config)
    if args.home:
        cfg = Config(
            **{**cfg.to_dict(), "home": Path(args.home).resolve()}
        )

    ensure_home(cfg.home)
    store = Store(cfg.home)
    audit = AuditLogger(cfg.home)
    audit.set_sink(store.record_event)
    engine = Engine(cfg, store, audit)

    data = Path(args.archive).read_bytes()
    verdict = engine.inspect(data, input_name=Path(args.archive).name)
    print(json.dumps(verdict.to_dict(), indent=2, sort_keys=True))
    store.close()
    return 0 if verdict.accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
