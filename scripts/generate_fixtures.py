#!/usr/bin/env python3
"""Materialize the synthetic scenarios as reusable raw .ts fixture files.

Writes tests/fixtures/data/<name>.ts so other tools (ffprobe, tsduck,
manual inspection, external fuzzers) can replay the exact bytes the test
suite uses, alongside a sidecar <name>.expect.json describing the result
the analyzer is required to produce.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import Settings  # noqa: E402
from app.demux import StreamAnalyzer  # noqa: E402
from tests.fixtures import ts_builder as tb  # noqa: E402

OUT_DIR = REPO_ROOT / "tests" / "fixtures" / "data"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, factory in sorted(tb.SCENARIOS.items()):
        scenario = factory()
        ts_path = OUT_DIR / f"{name}.ts"
        ts_path.write_bytes(scenario.data)

        result = StreamAnalyzer(Settings(), record_id=f"fixture:{name}").analyze(
            scenario.data
        )
        sidecar = {
            "name": name,
            "size_bytes": len(scenario.data),
            "expectation": scenario.expectation,
            "observed": {
                "packets_parsed": result.framing.packets_parsed,
                "bytes_skipped": result.framing.bytes_skipped,
                "leftover_bytes": result.framing.leftover_bytes,
                "fatal": result.framing.fatal,
                "diagnostic_codes": result.diagnostics.codes(),
                "programs": result.programs.snapshot(),
            },
        }
        (OUT_DIR / f"{name}.expect.json").write_text(
            json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n"
        )
        print(f"wrote {ts_path.name} ({len(scenario.data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
