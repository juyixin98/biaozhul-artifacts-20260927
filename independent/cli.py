"""CLI for the independent checker.

    python -m independent.cli evidence.json
    python -m independent.cli evidence.json --json

Exit code 0 = valid evidence, 1 = invalid/tampered, 2 = unreadable input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .checker import Verdict, review


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="independent-checker")
    p.add_argument("evidence", help="path to an evidence packet JSON file")
    p.add_argument("--json", action="store_true", help="machine-readable verdict")
    args = p.parse_args(argv)

    try:
        text = Path(args.evidence).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read evidence file: {exc}", file=sys.stderr)
        return 2

    verdict: Verdict = review(text)
    if args.json:
        print(json.dumps(verdict.as_dict(), indent=2))
    else:
        head = "VALID" if verdict.valid else f"INVALID ({verdict.reason})"
        print(f"evidence verdict: {head}")
        if verdict.validator:
            print(f"validator: {verdict.validator}")
        if verdict.offense:
            print(f"offense: {verdict.offense}")
        for check in verdict.checks:
            print(f"  - {check}")
        if verdict.detail:
            print(f"detail: {verdict.detail}")
    return 0 if verdict.valid else 1


if __name__ == "__main__":
    sys.exit(main())
