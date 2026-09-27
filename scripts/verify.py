#!/usr/bin/env python3
"""Standalone verification script.

Usage:
    python scripts/verify.py                          # run all generated
                                                      # fixtures + assertions
    python scripts/verify.py path/to/stream.ts [...]  # analyze arbitrary files

The script never trusts the analyzer for expected results: fixture assertions
come from the builder-derived ``*.expected.json`` manifests, and ad-hoc files
are reported with concrete finding categories rather than a raw pass/fail.
Checks that cannot be executed in this environment are listed separately and
never reported as passed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.analyzer import analyze  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "generated"


def ensure_fixtures() -> None:
    if not (FIX / "baseline.ts").exists():
        subprocess.run([sys.executable, "-m", "scripts.make_fixtures"],
                       cwd=ROOT, check=True)


def _has(report: dict, code: str) -> list[dict]:
    return [f for f in report["findings"] if f["code"] == code]


def verify_fixtures() -> tuple[int, int, list[str]]:
    """Return (passed, failed, not-executable notes)."""
    passed = failed = 0
    notes: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL {name}: {detail}")

    cases = {
        "baseline.ts": lambda d, m, r: [
            check("baseline.verdict", r["verdict"] == "accepted",
                  r["verdict"]),
            check("baseline.program", "768" in r["programs"]),
            check("baseline.streams",
                  {s["elementary_pid"]
                   for s in r["programs"]["768"]["streams"]}
                  == {0x1100, 0x1101}),
            check("baseline.3_pes",
                  sum(len(v) for v in r["pes"].values()) == 3),
            check("baseline.no_corrupt_pes",
                  all(not p["corrupt"]
                      for v in r["pes"].values() for p in v)),
        ],
        "duplicate.ts": lambda d, m, r: [
            check("duplicate.accepted",
                  _has(r, "duplicate_packet")
                  and _has(r, "duplicate_packet")[0]["disposition"]
                  == "accepted"),
            check("duplicate.pes_intact",
                  all(not p["corrupt"]
                      for v in r["pes"].values() for p in v)),
        ],
        "gap.ts": lambda d, m, r: [
            check("gap.cc_gap_rejected",
                  (g := _has(r, "cc_gap"))
                  and g[0]["disposition"] == "rejected"
                  and g[0]["details"]["missing_estimate"] == 3),
            check("gap.pes_gap_count", r["pes_gap_count"] == 1,
                  str(r["pes_gap_count"])),
            check("gap.first_pes_corrupt",
                  r["pes"][str(0x1100)][0]["corrupt"] is True),
        ],
        "adaptation_signaled.ts": lambda d, m, r: [
            check("signaled.accepted",
                  (s := _has(r, "signaled_discontinuity"))
                  and s[0]["disposition"] == "accepted"),
            check("signaled.no_cc_gap", not _has(r, "cc_gap")),
            check("signaled.no_adapt_mismatch",
                  not _has(r, "adaptation_only_cc_mismatch")),
            check("signaled.pes_intact",
                  all(not p["corrupt"]
                      for v in r["pes"].values() for p in v)),
        ],
        "cross_packet_pmt.ts": lambda d, m, r: [
            check("cross.full_map",
                  "768" in r["programs"]
                  and len(r["programs"]["768"]["streams"]) >= 2),
            check("cross.no_crc_error", not _has(r, "section_crc_error")),
        ],
        "bad_crc.ts": lambda d, m, r: [
            check("badcrc.rejected", r["verdict"] == "rejected"),
            check("badcrc.crc_error", bool(_has(r, "section_crc_error"))),
            check("badcrc.no_map", r["programs"] == {}),
            check("badcrc.unknown_pid", bool(_has(r, "unknown_pid_payload"))),
        ],
        "version_switch.ts": lambda d, m, r: [
            check("version.pat_v2", r["pat"]["version"] == 2),
            check("version.two_switches",
                  len(_has(r, "table_version_switch")) == 2),
            check("version.video_only",
                  [s["elementary_pid"]
                   for s in r["programs"]["768"]["streams"]] == [0x1100]),
        ],
        "resync.ts": lambda d, m, r: [
            check("resync.one_resync", r["resyncs"] == 1),
            check("resync.skipped_bytes",
                  r["skipped_bytes"] == m["garbage_bytes"]),
            check("resync.map_survives", bool(r["programs"])),
        ],
    }

    for fname, fn in cases.items():
        path = FIX / fname
        data = path.read_bytes()
        manifest = json.loads(path.with_suffix(".expected.json").read_text())
        rid = f"verify-{fname.removesuffix('.ts')}"
        report = analyze(data, rid).to_dict()
        print(f"[{fname}] verdict={report['verdict']} "
              f"packets={report['parsed_packets']}")
        fn(data, manifest, report)

    # Checks that cannot be executed in this offline environment are listed,
    # never silently counted as passed.
    notes.append(
        "real-world broadcast TS capture: not included (only synthetic "
        "local fixtures are used by design)")
    notes.append(
        "hardware PCR jitter tolerance against a physical demodulator: not "
        "executable in CI; PCR monotonicity is checked on synthetic PCRs")
    return passed, failed, notes


def analyze_file(path: Path) -> int:
    data = path.read_bytes()
    report = analyze(data, f"file-{path.name}").to_dict()
    summary = {
        "file": path.name,
        "bytes": report["total_bytes"],
        "packets": report["parsed_packets"],
        "verdict": report["verdict"],
        "programs": list(report["programs"]),
        "resyncs": report["resyncs"],
        "pes_gap_count": report["pes_gap_count"],
        "findings": [
            {"code": f["code"], "severity": f["severity"],
             "disposition": f["disposition"], "pid": f["pid"],
             "packet_index": f["packet_index"]}
            for f in report["findings"]
            if f["severity"] in ("warning", "error")
        ],
    }
    print(json.dumps(summary, indent=2))
    return 0 if report["verdict"] != "rejected" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="MPEG-TS verification")
    parser.add_argument("files", nargs="*", help="optional .ts files")
    parser.add_argument("--json", action="store_true",
                        help="emit the full report JSON for ad-hoc files")
    args = parser.parse_args()

    ensure_fixtures()
    if args.files:
        rc = 0
        for name in args.files:
            rc |= analyze_file(Path(name))
        return rc

    print("== Synthetic fixture verification ==")
    passed, failed, notes = verify_fixtures()
    print(f"\npassed={passed} failed={failed}")
    print("\nNot executable in this environment (NOT counted as passed):")
    for n in notes:
        print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
