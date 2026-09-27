"""Console entry point: ``clockalign-fixtures --out samples``."""
from __future__ import annotations

import argparse

from . import SCENARIOS, generate


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate independent synthetic clock-drift fixtures")
    parser.add_argument("--out", default="samples",
                        help="output directory (default: ./samples)")
    parser.add_argument("--only", nargs="*", choices=sorted(SCENARIOS),
                        help="generate only these scenarios")
    args = parser.parse_args()
    index = generate(args.out, names=tuple(args.only) if args.only else None)
    for name, paths in index.items():
        print(f"{name:18s} -> {paths['wav']}")


if __name__ == "__main__":
    main()
