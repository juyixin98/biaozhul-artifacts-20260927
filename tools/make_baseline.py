#!/usr/bin/env python3
"""Independent baseline fingerprint generator — stdlib ONLY.

This tool deliberately imports nothing from :mod:`secretscan`. It re-implements
the fingerprint construction with the standard library so reference answers in
baselines and tests are produced independently of the code under test; the test
suite cross-checks that the two implementations agree.

Usage
-----
Print the pepper id (to put in the baseline [meta] section)::

    python tools/make_baseline.py --pepper-id

Append an [[exemptions]] entry for a literal secret value::

    python tools/make_baseline.py --value 'ghp_...' --rule-id github-classic-pat \
        --note 'legacy demo key'

Read the value from a file instead (avoids shell history)::

    python tools/make_baseline.py --value-file path/to/value.txt --rule-id ...
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import sys
from pathlib import Path

# Must match config.py's development default ONLY for local fixture work.
DEFAULT_DEV_PEPPER = "dev-pepper-do-not-use-in-production-opp275"


def fingerprint(value: str, pepper: str) -> str:
    return hmac.new(pepper.encode("utf-8"), value.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def mask(value: str) -> str:
    """Independent copy of the masking rule (keep 4 at each end, 12+ chars)."""
    n = len(value)
    if n == 0:
        return ""
    if n <= 12:
        return "*" * n
    keep = min(4, (n - 4) // 2)
    return f"{value[:keep]}{'*' * (n - 2 * keep)}{value[-keep:]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate an HMAC fingerprint + mask for a baseline.")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--value", help="literal secret value (fake fixtures only)")
    src.add_argument("--value-file", help="file containing the value")
    src.add_argument("--pepper-id", action="store_true",
                     help="print the pepper id and exit")
    parser.add_argument("--rule-id", default=None,
                        help="optional rule id to narrow the exemption")
    parser.add_argument("--note", default="", help="human note")
    parser.add_argument("--pepper-env", default="SECRETSCAN_PEPPER",
                        help="env var name holding the pepper override")
    args = parser.parse_args(argv)

    import os
    pepper = os.environ.get(args.pepper_env, DEFAULT_DEV_PEPPER)
    pepper_id = hashlib.sha256(pepper.encode("utf-8")).hexdigest()[:12]
    if args.pepper_id:
        print(pepper_id)
        return 0

    if args.value_file:
        value = Path(args.value_file).read_text(encoding="utf-8").strip()
    else:
        value = args.value
    fp = fingerprint(value, pepper)
    out = [
        "[[exemptions]]",
        f'fingerprint = "{fp}"',
        f'mask = "{mask(value)}"',
    ]
    if args.rule_id:
        out.append(f'rule_id = "{args.rule_id}"')
    if args.note:
        out.append(f'note = "{args.note}"')
    print("\n".join(out))
    print(f"# pepper_id={pepper_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
