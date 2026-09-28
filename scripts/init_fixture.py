"""Initialize the local synthetic fixture DB from samples/fixture/*.sql."""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlguard.state.fixture import create_fixture


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture-dir", default="samples/fixture")
    args = ap.parse_args()
    d = Path(args.fixture_dir)
    schema = (d / "schema.sql").read_text(encoding="utf-8")
    seed_path = d / "seed.sql"
    seed = seed_path.read_text(encoding="utf-8") if seed_path.exists() else None
    out = create_fixture(d / "fixture.db", schema, seed)
    print(f"fixture written: {out}")


if __name__ == "__main__":
    main()
