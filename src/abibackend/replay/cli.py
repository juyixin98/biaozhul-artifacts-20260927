"""Offline replay CLI.

Usage:
    python -m abibackend.replay.cli [--db PATH] [--reset] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys

from ..config import SETTINGS
from ..log_utils import configure_logging
from ..storage import Repository
from .engine import replay


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline synthetic replay")
    parser.add_argument("--db", default=SETTINGS.db_path, help="SQLite path")
    parser.add_argument("--reset", action="store_true", help="wipe store before replay")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    args = parser.parse_args(argv)

    repo = Repository(args.db)
    if args.reset:
        repo.reset()
    try:
        report = replay(repo, mode="scenario")
    finally:
        repo.close()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(f"run_id={report.run_id} total={report.total} "
              f"ok={report.succeeded} failed={report.failed}")
        for s in report.steps:
            mark = "OK  " if s.ok else "FAIL"
            extra = s.call if s.ok else f"{s.error_code}: {s.error_message}"
            print(f"  [{s.index}] {mark} nonce={s.nonce} {extra}")
        print(f"state_root={report.final_state_root}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
