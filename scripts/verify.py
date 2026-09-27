#!/usr/bin/env python3
"""End-to-end verification harness for the three-way merge backend.

Runs without pytest by importing the package from ``src/``.  It executes a
fixed set of scenarios whose expected answers are literal strings in this
file and in ``fixtures/merge_cases.json`` (hand-authored, not produced by
the engine), prints one line per check, and exits non-zero on any failure.

It also demonstrates, and asserts, that diagnostics containing a
credential-shaped fixture value never leak the secret into the log.

Usage:
    python scripts/verify.py
"""

from __future__ import annotations

import json
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from merge3 import three_way_merge  # noqa: E402
from merge3.config import Settings  # noqa: E402
from merge3.diagnostics import redact_sensitive  # noqa: E402
from merge3.service import MergeService  # noqa: E402
from merge3.storage import VersionStore  # noqa: E402

# Ensure DEBUG logging is visible on stderr so the redaction check has real
# output to inspect.
logging.basicConfig(level=logging.DEBUG, format="%(message)s")

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name}  {detail}")


def scenario(case_id, description, base, local, remote, *,
             auto, merged=None, conflict_types=None, resolutions=None,
             redaction=None):
    print(f"[{case_id}] {description}")
    engine, result = three_way_merge(base, local, remote, f"req-{case_id}")
    check("auto flag", result.auto_merged is auto,
          f"got {result.auto_merged} conflicts="
          f"{[c.conflict_type.value for c in result.conflicts]}")
    if auto:
        check("exact merged text", result.merged_text == merged,
              f"got {result.merged_text!r} want {merged!r}")
    else:
        got_types = [c.conflict_type.value for c in result.conflicts]
        check("conflict categories", got_types == list(conflict_types or []),
              f"got {got_types}")
        check("no guessed merged text", result.merged_text is None)
        if resolutions:
            for choice, expected in resolutions.items():
                if choice == "custom_text":
                    rebuilt = engine.rebuild(
                        result, {"c1": {"choice": "custom_text", "text": expected}})
                else:
                    rebuilt = engine.rebuild(result, {"c1": {"choice": choice}})
                check(f"rebuild[{choice}]", rebuilt == expected,
                      f"got {rebuilt!r} want {expected!r}")
    # diagnostics presence
    check("diagnostics explain decision",
          any(d["outcome"] in ("accepted", "conflict")
              for d in engine.decisions))
    check("diagnostics carry request id",
          all(d.get("request_id") == f"req-{case_id}"
              for d in engine.decisions if "cluster_span" in d))


def run_service_roundtrip():
    print("[service] SQLite + service round-trip with secret redaction")
    settings = Settings(database_path=":memory:", log_redact_secrets=True)
    store = VersionStore(":memory:")
    svc = MergeService(store, settings)
    fixture_path = os.path.join(ROOT, "fixtures", "sensitive_demo.json")
    data = json.load(open(fixture_path, encoding="utf-8"))
    response = svc.run_merge(
        data["base_text"], data["local_text"], data["remote_text"],
        document_id=data["document_id"], request_id=data["request_id"])
    check("service clean merge", response.status == "auto")
    blob = json.dumps(response.diagnostics)
    check("secret never printed",
          data["fake_secret"] not in blob and data["fake_token"] not in blob,
          "a credential-shaped value leaked into diagnostics")
    check("redaction helper masks secret",
          data["fake_secret"] not in
          json.dumps(redact_sensitive({"api_key": data["fake_secret"]})))
    record = svc.get_merge(response.merge_id)
    check("merge persisted with provenance",
          record["status"] == "auto" and record["merged_text"]
          == response.merged_text)
    store.close()


def run_json_fixtures():
    print("[json-fixtures] asserting fixtures/merge_cases.json literals")
    path = os.path.join(ROOT, "fixtures", "merge_cases.json")
    data = json.load(open(path, encoding="utf-8"))
    for case in data["cases"]:
        engine, result = three_way_merge(case["base"], case["local"],
                                         case["remote"], f"req-{case['case_id']}")
        exp = case["expect"]
        if exp["auto"]:
            check(f"{case['case_id']}:json auto text",
                  result.auto_merged and result.merged_text == exp["merged_text"],
                  f"got {result.merged_text!r}")
        else:
            got = [c.conflict_type.value for c in result.conflicts]
            check(f"{case['case_id']}:json conflict types",
                  not result.auto_merged and got == exp["conflict_types"],
                  f"got {got}")


def main() -> int:
    scenario(
        "move_disjoint", "moved similar paragraphs + remote edit",
        "p1\np2\np3\np4\np5\np6\n",
        "p2\np1\np3\np4\np5\np6\n",
        "p1\np2\np3\np4\np5\nP6SIX\ntail\n",
        auto=True,
        merged="p2\np1\np3\np4\np5\nP6SIX\ntail\n",
    )
    scenario(
        "duplicate_lines", "repeated line run edited on both sides",
        "r\nr\nr\nr\n", "r\nL1\nr\nr\n", "r\nr\nR1\nr\n",
        auto=True, merged="r\nL1\nR1\nr\n",
    )
    scenario(
        "same_point_insert", "different inserts at the same boundary",
        "h1\nh2\nh3\n",
        "INS-L\nh1\nh2\nh3\n", "INS-R\nh1\nh2\nh3\n",
        auto=False, conflict_types=["same_point_insert"],
        resolutions={
            "local": "INS-L\nh1\nh2\nh3\n",
            "remote": "INS-R\nh1\nh2\nh3\n",
            "base": "h1\nh2\nh3\n",
            "local_then_remote": "INS-L\nINS-R\nh1\nh2\nh3\n",
            "remote_then_local": "INS-R\nINS-L\nh1\nh2\nh3\n",
        },
    )
    scenario(
        "delete_modify", "one side deletes, the other modifies",
        "alpha\nbeta\ngamma\ndelta\n",
        "alpha\ngamma\ndelta\n", "alpha\nBETA!\ngamma\ndelta\n",
        auto=False, conflict_types=["delete_modify"],
        resolutions={
            "local": "alpha\ngamma\ndelta\n",
            "remote": "alpha\nBETA!\ngamma\ndelta\n",
            "base": "alpha\nbeta\ngamma\ndelta\n",
        },
    )
    scenario(
        "eol_fidelity", "CRLF/LF and missing trailing newline preserved",
        "one\r\ntwo\r\nthree\r\n",
        "one\r\nTWO\r\nthree\r\n",
        "one\r\ntwo\r\nthree\r\nfour\r\n",
        auto=True, merged="one\r\nTWO\r\nthree\r\nfour\r\n",
    )
    scenario(
        "no_trailing_newline", "result must not gain a trailing newline",
        "a\nb\nc", "a\nb\nc\nd", "A\nb\nc",
        auto=True, merged="A\nb\nc\nd",
    )
    scenario(
        "partial_overlap", "overlapping non-coincident ranges",
        "l1\nl2\nl3\nl4\nl5\n",
        "l1\nX\nY\nl4\nl5\n", "l1\nl2\nZ\nl4\nl5\n",
        auto=False, conflict_types=["partial_overlap"],
        resolutions={
            "local": "l1\nX\nY\nl4\nl5\n",
            "remote": "l1\nl2\nZ\nl4\nl5\n",
            "base": "l1\nl2\nl3\nl4\nl5\n",
        },
    )
    scenario(
        "insert_inside_range", "insert at boundary strictly inside replace",
        "a\nb\nc\nd\n",
        "a\nB\nC\nd\n", "a\nb\nINS\nc\nd\n",
        auto=False, conflict_types=["insert_range"],
        resolutions={
            "local": "a\nB\nC\nd\n",
            "remote": "a\nb\nINS\nc\nd\n",
            "base": "a\nb\nc\nd\n",
        },
    )

    run_service_roundtrip()
    run_json_fixtures()

    print("\n" + "=" * 60)
    print(f"TOTAL: {PASS} passed, {FAIL} failed")
    if FAILURES:
        print("failed checks:")
        for name in FAILURES:
            print("  -", name)
        return 1
    print("ALL VERIFICATION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
