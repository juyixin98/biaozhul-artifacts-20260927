#!/usr/bin/env python3
"""Verify a disclosure package JSON file with the independent verifier.

Usage:
    python scripts/verify_package.py path/to/package.json

Exit code: 0 = VALID, 2 = verification failed, 1 = malformed/unreadable input.
The progress trace is printed to stderr so stdout stays machine-parseable.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.verifier.independent import VerifyStatus, verify_package


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: verify_package.py PACKAGE.json", file=sys.stderr)
        return 1
    try:
        with open(argv[1], "r", encoding="utf-8") as fh:
            package = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"verdict": "MALFORMED_PACKAGE", "error": str(exc)}))
        return 1

    report = verify_package(package, progress=lambda m: print(m, file=sys.stderr))
    print(json.dumps(report.to_dict(), indent=2))
    if report.verdict == VerifyStatus.VALID:
        return 0
    return 2 if report.verdict in (
        VerifyStatus.COMMITMENT_MISMATCH, VerifyStatus.PROOF_INVALID,
        VerifyStatus.ROOT_MISMATCH, VerifyStatus.FIELD_IDENTITY_MISMATCH,
        VerifyStatus.CELL_SET_MISMATCH) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
