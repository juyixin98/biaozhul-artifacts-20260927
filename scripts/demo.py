#!/usr/bin/env python3
"""Local end-to-end demo: runs every bundled synthetic fixture through the
same pipeline the HTTP API uses and prints a per-case verdict.

Usage:
    python scripts/demo.py
    python scripts/demo.py --fixture tests/fixtures/chain_overlap.srt

No network access or external accounts are required.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402
from app.services.pipeline import run_validation  # noqa: E402

FIX = ROOT / "tests" / "fixtures"

# (file, settings overrides, human description)
CASES = [
    ("chain_overlap.srt", {}, "chained A>B>C overlaps"),
    ("same_start.vtt", {}, "identical start + negative/zero durations"),
    ("multilingual.srt", {}, "multi-script text + boundary crossing"),
    ("negative_duration.srt", {}, "negative / zero / too-short cues"),
    ("clean.srt", {}, "fully legal document"),
    ("bad_timestamp.srt", {}, "strict timestamp rejection"),
    ("bad_markup.vtt", {}, "unsupported/mismatched markup"),
    ("srt_position.srt", {}, "positioning metadata outside subset"),
    ("bad_encoding.srt", {}, "invalid UTF-8 bytes"),
    ("unfixable.srt", {"max_per_cue_shift_ms": 300},
     "unrepairable time window (must NOT be force-squeezed)"),
    ("budget_exceeded.srt",
     {"max_per_cue_shift_ms": 2_000, "segment_boundaries_ms": (60_000,),
      "max_total_shift_ms": 2_000},
     "feasible but minimal displacement exceeds budget"),
]


def _line(s: str = "") -> None:
    print(s)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path,
                        help="run a single fixture file instead of the full set")
    parser.add_argument("--quiet-logs", action="store_true",
                        help="reduce engine logging to WARNING")
    args = parser.parse_args()
    configure_logging("WARNING" if args.quiet_logs else "INFO")

    if args.fixture:
        cases = [(str(args.fixture), {}, str(args.fixture))]
    else:
        cases = [(str(FIX / name), over, desc) for name, over, desc in CASES]

    failures = 0
    for path_s, overrides, desc in cases:
        path = Path(path_s)
        data = path.read_bytes()
        settings = Settings(**overrides)
        _line("=" * 78)
        _line(f"FILE     {path.name}  — {desc}")
        _line(f"BYTES    {len(data)}  FORMAT(auto)  RUN-ID logged above")
        result = run_validation(data, settings=settings,
                                run_id=f"demo-{path.stem}")
        _line(f"STATUS   {result.status}   cues={result.cue_count}   "
              f"elapsed={result.elapsed_ms:.2f}ms")
        _line(f"MESSAGE  {result.message}")
        if result.failure_codes:
            _line(f"FAILURES {result.failure_codes}")
        codes: dict[str, int] = {}
        for d in result.diagnostics:
            codes[d.code] = codes.get(d.code, 0) + 1
        if codes:
            _line("DIAGNOSTICS " + ", ".join(f"{k}x{v}" for k, v in
                                             sorted(codes.items())))
        if result.repair:
            rep = result.repair
            _line(f"REPAIR   total_shift={rep['total_shift_ms']}ms "
                  f"max_shift={rep['max_shift_ms']}ms "
                  f"budget={rep['budget_ms']}ms cues={len(rep['cues'])}")
            moved = [c for c in rep["cues"] if c["action"] != "held"]
            for c in moved[:4]:
                _line(f"         cue#{c['cue_index'] + 1} {c['action']:>15} "
                      f"{c['original_start_ms']}->{c['repaired_start_ms']}ms "
                      f"shift={c['shift_ms']:+d} reasons={c['reasons']}")
            if len(moved) > 4:
                _line(f"         ... and {len(moved) - 4} more moved cue(s)")
            assert result.repaired_document is not None
            assert result.repaired_document.count("-->") == result.cue_count, \
                "cues must never be silently dropped"
        else:
            assert result.repaired_document is None

        # The demo asserts its own expected outcomes:
        expect = _EXPECTED.get(path.name)
        if expect is not None and result.status not in expect:
            _line(f"!! DEMO ASSERTION FAILED: expected {expect}, "
                  f"got {result.status}")
            failures += 1

    _line("=" * 78)
    if failures:
        _line(f"DEMO FAILED with {failures} unexpected outcome(s)")
        return 1
    _line("ALL DEMO CASES BEHAVED AS DOCUMENTED")
    return 0


# Statuses each shipped fixture is expected to produce.
_EXPECTED = {
    "chain_overlap.srt": {"repaired"},
    "same_start.vtt": {"repaired"},
    "multilingual.srt": {"repaired"},
    "negative_duration.srt": {"repaired"},
    "clean.srt": {"clean"},
    "bad_timestamp.srt": {"parse_failed"},
    "bad_markup.vtt": {"parse_failed"},
    "srt_position.srt": {"parse_failed"},
    "bad_encoding.srt": {"parse_failed"},
    "unfixable.srt": {"infeasible_bounds"},
    "budget_exceeded.srt": {"budget_exceeded"},
}


if __name__ == "__main__":
    raise SystemExit(main())
