#!/usr/bin/env python3
"""Generate the bundled sample fixtures into ./samples (or $1)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from fixturegen import SCENARIOS, generate  # noqa: E402

if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples"
    only = tuple(sys.argv[2:]) or None
    index = generate(out, names=only)
    print(f"wrote {len(index)} scenarios to {out}:")
    for name, paths in index.items():
        print(f"  {name:18s} {Path(paths['wav']).name}  "
              f"{SCENARIOS[name].description}")
