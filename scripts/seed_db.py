"""Seed a dictionary version from a local synthetic fixture.

File format: one ``term`` or ``term<TAB>frequency`` per non-empty, non-# line.
Normalization is applied to terms so they are directly comparable to
normalized queries.

Usage::

    python -m scripts.seed_db --config config/default.json --activate
    python -m scripts.seed_db --file examples/seed_words.txt --description "v2"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python -m scripts.seed_db` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.normalization import normalize_char  # noqa: E402
from app.storage import VersionStore  # noqa: E402


def parse_seed_file(path: str | Path, alphabet: frozenset[str]) -> list[tuple[str, float]]:
    entries: list[tuple[str, float]] = []
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        term_raw = parts[0]
        freq = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
        term = "".join(normalize_char(c) for c in term_raw)
        bad = [c for c in term if not c.isspace() and c not in alphabet]
        if bad:
            raise ValueError(f"{path}:{lineno}: term {term!r} has unsupported chars {bad}")
        entries.append((term, freq))
    return entries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/default.json")
    parser.add_argument("--file", default=None)
    parser.add_argument("--description", default="seeded fixture")
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()

    settings = load_settings(args.config)
    seed_file = args.file or settings.seed_file
    entries = parse_seed_file(seed_file, settings.alphabet_set)
    store = VersionStore(settings.db_path)
    vid = store.create_version(entries, description=args.description, activate=args.activate)
    print(f"created version_id={vid} with {len(entries)} entries from {seed_file}")


if __name__ == "__main__":
    main()
