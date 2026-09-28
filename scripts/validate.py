#!/usr/bin/env python3
"""Standalone validation runner for the local MPEG-TS analyzer.

Runs every synthetic scenario through the real analyzer (no HTTP server
needed) and asserts the scenario's expectations, printing one line per
check with PASS/FAIL and the key state. Exit code 0 only if every check
passes.

Usage:
    python scripts/validate.py [--scenario NAME] [--verbose]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import Settings  # noqa: E402
from app.demux import StreamAnalyzer  # noqa: E402
from tests.fixtures import ts_builder as tb  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"


class CheckLog:
    def __init__(self) -> None:
        self.failures = 0
        self.total = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.total += 1
        if not ok:
            self.failures += 1
        mark = PASS if ok else FAIL
        suffix = f"  [{detail}]" if detail else ""
        print(f"  {mark}  {name}{suffix}")


def analyze(data: bytes, record_id: str):
    return StreamAnalyzer(Settings(), record_id=record_id).analyze(data)


def run_scenario(name: str, log: CheckLog, verbose: bool) -> None:
    scenario = tb.SCENARIOS[name]()
    exp = scenario.expectation
    result = analyze(scenario.data, record_id=f"validate:{name}")
    codes = result.diagnostics.codes()
    snap = result.programs.snapshot()
    print(f"scenario: {name} ({len(scenario.data)} bytes)")

    if "programs" in exp:
        got = (
            {p["program_number"]: p["pid"] for p in snap["pat"]["programs"]}
            if snap["pat"] else {}
        )
        log.check("program map", got == exp["programs"], f"got={got}")
    if "streams" in exp:
        got = result.programs.es_pids()
        want = exp["streams"]
        log.check("elementary streams", all(got.get(p) == t for p, t in want.items()),
                  f"got={got}")
    if "pmt_version" in exp:
        pmts = snap["pmts"]
        log.check("pmt version", bool(pmts) and pmts[0]["version"] == exp["pmt_version"])
    if "pcr_pid" in exp:
        pmts = snap["pmts"]
        log.check("pcr pid", bool(pmts) and pmts[0]["pcr_pid"] == exp["pcr_pid"])
    if "pes" in exp:
        records = result.pes.records
        for want in exp["pes"]:
            got = [r for r in records if r.pid == want["pid"]]
            ok = (
                len(got) == 1
                and got[0].payload_bytes == want["payload_bytes"]
                and got[0].complete == want["complete"]
                and got[0].header is not None
                and got[0].header.pts == want["pts"]
                and got[0].gap == want["gap"]
            )
            log.check(f"pes pid={want['pid']:#x}", ok,
                      f"got={[(r.payload_bytes, r.complete, r.gap) for r in got]}")
    if "duplicates" in exp:
        for pid, count in exp["duplicates"].items():
            got = result.continuity.state_for(pid).duplicates
            log.check(f"duplicates pid={pid:#x}", got == count, f"got={got}")
    if "lost" in exp:
        for pid, count in exp["lost"].items():
            got = result.continuity.state_for(pid).lost
            log.check(f"lost pid={pid:#x}", got == count, f"got={got}")
    if "pes_complete" in exp:
        records = [r for r in result.pes.records if r.pid == tb.VIDEO_PID]
        log.check("pes complete", bool(records) and records[0].complete == exp["pes_complete"])
    if "pes_gap" in exp:
        records = [r for r in result.pes.records if r.pid == tb.VIDEO_PID]
        log.check("pes gap", bool(records) and records[0].gap == exp["pes_gap"])
    if "garbage_prefix" in exp:
        rec = [e for e in result.diagnostics.events if e.code == "sync_recovered"]
        log.check("sync recovered twice", len(rec) == 2, f"got={len(rec)}")
        if rec:
            log.check("prefix skipped", rec[0].context["skipped_bytes"] == exp["garbage_prefix"])
            log.check("middle skipped", rec[1].context["skipped_bytes"] == exp["garbage_middle"])
        log.check("trailing leftover", result.framing.leftover_bytes == exp["trailing"],
                  f"got={result.framing.leftover_bytes}")
        log.check("all packets parsed",
                  result.framing.packets_parsed == exp["clean_packet_count"],
                  f"got={result.framing.packets_parsed}")
    if "codes_present" in exp:
        for code in exp["codes_present"]:
            log.check(f"code present: {code}", code in codes)
    if "codes_absent" in exp:
        for code in exp["codes_absent"]:
            log.check(f"code absent: {code}", code not in codes)
    if "declared_discontinuities" in exp:
        for pid, count in exp["declared_discontinuities"].items():
            got = result.continuity.state_for(pid).declared_discontinuities
            log.check(f"declared discontinuities pid={pid:#x}", got == count, f"got={got}")
    if "crc_errors" in exp:
        got = codes.count("table_crc_error")
        log.check("crc errors", got == exp["crc_errors"], f"got={got}")
    if "section_incomplete" in exp:
        got = codes.count("section_incomplete")
        log.check("section incomplete", got == exp["section_incomplete"], f"got={got}")
    if "table_not_current" in exp:
        got = codes.count("table_not_current")
        log.check("table not current", got == exp["table_not_current"], f"got={got}")
    if "final_program" in exp:
        pnum, pid = exp["final_program"]
        progs = {p["program_number"]: p["pid"] for p in snap["pat"]["programs"]}
        log.check("final program map", progs.get(pnum) == pid, f"got={progs}")
    if "final_streams" in exp:
        got = result.programs.es_pids()
        log.check("final streams", got == exp["final_streams"], f"got={got}")
    if "final_pcr_pid" in exp:
        pmts = snap["pmts"]
        log.check("final pcr pid", bool(pmts) and pmts[0]["pcr_pid"] == exp["final_pcr_pid"])
    if "pat_version_switches" in exp:
        got = codes.count("pat_version_switch")
        log.check("pat version switches", got == exp["pat_version_switches"], f"got={got}")
    if "pmt_version_switches" in exp:
        got = codes.count("pmt_version_switch")
        log.check("pmt version switches", got == exp["pmt_version_switches"], f"got={got}")
    if "retired_pmt_pid" in exp:
        pmts = {p["pid"] for p in snap["pmts"]}
        log.check("retired pmt pid", exp["retired_pmt_pid"] not in pmts, f"pmts={pmts}")
    if "pcr_samples" in exp:
        stats = result.timing.snapshot().get(exp["pcr_pid"])
        log.check("pcr samples", stats is not None and stats["samples"] == exp["pcr_samples"])
    if "mean_interval_ms" in exp:
        stats = result.timing.snapshot().get(exp["pcr_pid"])
        ok = stats is not None and abs(stats["mean_pcr_interval_ms"] - exp["mean_interval_ms"]) < 0.01
        log.check("mean pcr interval", ok, f"got={stats and stats['mean_pcr_interval_ms']}")

    if verbose:
        for event in result.diagnostics.events:
            print(f"    [{event.severity:7}] {event.code} pid={event.pid} "
                  f"offset={event.offset} {event.message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(tb.SCENARIOS), default=None,
                        help="run a single scenario")
    parser.add_argument("--verbose", action="store_true",
                        help="print every diagnostic event per scenario")
    args = parser.parse_args()

    names = [args.scenario] if args.scenario else sorted(tb.SCENARIOS)
    log = CheckLog()
    for name in names:
        run_scenario(name, log, args.verbose)

    print()
    print(f"{log.total - log.failures}/{log.total} checks passed")
    if log.failures:
        print(f"{log.failures} check(s) FAILED")
        return 1
    print("all scenario checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
