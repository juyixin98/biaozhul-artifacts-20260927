"""Replay a diagnostics JSONL log.

Usage::

    python -m scripts.replay logs/service.jsonl
    python -m scripts.replay tests/_run_artifacts/<run>.tests.jsonl --summary

For service logs it prints each op's category and verifies error records are
well-formed; for test logs it re-derives expected index tables from the
independent oracle and re-checks the recorded verdicts.  Exit code is the
number of inconsistent records (0 = clean replay).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import oracle  # noqa: E402

VALID_OUTCOMES = {
    "ok", "input_error", "state_conflict",
    "resource_exhausted", "computation_failure",
}


def load(path: Path):
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if line:
                yield lineno, json.loads(line)


def replay_service(path: Path) -> int:
    problems = 0
    counts = {o: 0 for o in VALID_OUTCOMES}
    for lineno, rec in load(path):
        outcome = rec.get("outcome")
        if outcome not in VALID_OUTCOMES:
            print(f"line {lineno}: unknown outcome {outcome!r}")
            problems += 1
            continue
        counts[outcome] += 1
        if outcome != "ok" and not rec.get("code"):
            print(f"line {lineno}: failure without code: {rec}")
            problems += 1
        if not rec.get("run_id") or "seq" not in rec:
            print(f"line {lineno}: missing run_id/seq")
            problems += 1
    print(f"service log {path}: {counts}")
    return problems


def replay_tests(path: Path) -> int:
    problems = 0
    verdicts = {"PASS": 0, "FAIL": 0}
    for lineno, rec in load(path):
        verdicts[rec.get("verdict", "?")] = \
            verdicts.get(rec.get("verdict", "?"), 0) + 1
        if rec.get("verdict") != "PASS":
            problems += 1
            print(f"run_no {rec.get('run_no')} {rec.get('test')}: "
                  f"{rec.get('reason')}")
        # Re-derive expected tables for recorded hand/property judgements
        # when the intermediate carried the input codepoints.
        inter = rec.get("intermediate") or {}
        # Prefer the full replay sequence; fall back to the (possibly
        # truncated) preview only when no truncation sentinel is present.
        cps = inter.get("replay_codepoints") or inter.get("codepoints")
        truncated = isinstance(cps, list) and any(
            isinstance(c, str) and c.startswith("…") for c in cps)
        if cps and isinstance(cps, list) and rec.get("kind") == "property" \
                and not truncated:
            text = "".join(_cp_to_chr(c) for c in cps if isinstance(c, str))
            if text:
                exp = oracle.expected_index(text)
                recorded_exp = rec.get("expected")
                if isinstance(recorded_exp, dict):
                    for key in ("cp_starts", "byte_starts"):
                        if exp.get(key) != recorded_exp.get(key):
                            problems += 1
                            print(f"run_no {rec.get('run_no')}: replay "
                                  f"oracle divergence on {key}")
    print(f"test log {path}: {verdicts}")
    return problems


def _cp_to_chr(token: str) -> str:
    return chr(int(token.replace("U+", ""), 16))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--summary", action="store_true",
                        help="treat the log as a test judgement log")
    args = parser.parse_args(argv)
    if not args.log.exists():
        print(f"log not found: {args.log}", file=sys.stderr)
        return 2
    if args.summary:
        return replay_tests(args.log)
    # auto-detect: test logs carry "run_no"/"verdict"
    first = next(load(args.log), (0, {}))[1]
    if "verdict" in first:
        return replay_tests(args.log)
    return replay_service(args.log)


if __name__ == "__main__":
    raise SystemExit(main())
