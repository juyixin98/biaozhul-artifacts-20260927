#!/usr/bin/env python3
"""End-to-end verification script (run via scripts/verify.sh).

Exercises the acceptance scenarios against throwaway synthetic snapshots:

1. artificial fake keys are detected (structure + entropy)
2. ordinary high-entropy text is NOT flagged (entropy alone insufficient)
3. a moved candidate is classified "moved"
4. a deleted candidate is "uncertain_removal", never "fixed"
5. a replaced-in-place candidate is "known_fixed"
6. oversize files are explicitly unscanned
7. the raw secret never appears in report JSON, SQLite DB or log file
8. audit log is redacted and carries request identity

Every check prints PASS/FAIL with its assertion; the script exits non-zero on
the first failure. Network validation of credentials is deliberately NOT
performed and is listed as an intentional boundary at the end.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from secretscan import audit, service, state  # noqa: E402
from secretscan.config import load_rule_pack, load_scope_pack  # noqa: E402
from secretscan.security import Fingerprinter  # noqa: E402

RULES = ROOT / "config" / "rules" / "default.toml"
SCOPE = ROOT / "tests" / "config" / "scope-tiny.toml"

GHP = "ghp_1eAoPJ4BzuZNn3XmX7lgARsGjSQZTBCSEIka"
THIRD = "ghp_ogpZeYrOl8JQ5SzSWCAjDBW4sxXq7PLIhG0a"
SLACK = ("xoxb-123456789012-1234567890123-"
         "AbCdEfGhIjKlMnOpQrStUvWx")
PROSE = "Xk9pL2vQ7mR4nT8wY3cB6hJ1fD0sG5aZ"

failures = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global failures
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures += 1


def states(report) -> dict[str, str]:
    return {f.rule_id: f.state
            for fl in report.findings.values() for f in fl}


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="secretscan-verify-"))
    try:
        repo = work / "repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "src" / "app.py").write_text(
            f'GITHUB_TOKEN = "{GHP}"\n'
            f'SLACK_BOT_TOKEN = "{SLACK}"\n')
        (repo / "src" / "removeme.py").write_text(f'TOKEN = "{THIRD}"\n')
        (repo / "notes.txt").write_text(
            f"random identifier, not assigned: {PROSE}\n")
        (repo / "huge.log").write_bytes(b"x" * 1024)  # > 512-byte tiny scope

        db = work / "ws.db"
        log_file = work / "audit.log"
        rules = load_rule_pack(RULES)
        scope = load_scope_pack(SCOPE)
        fingerprinter = Fingerprinter("dev-pepper-do-not-use-in-production-opp275")
        logger, _ = audit.configure_logging(log_file)
        conn = state.connect(db)
        svc = service.ScanService(conn, rules, scope, fingerprinter, logger)

        r1 = svc.run_scan(repo, audit.RequestContext.create("verify-1", "ci"))
        s1 = states(r1)
        check("1a fake GitHub token detected",
              s1.get("github-classic-pat") == "new", str(s1))
        check("1b fake Slack token detected",
              s1.get("slack-bot-token") == "new", str(s1))
        check("1c high-entropy prose without structure is NOT a candidate",
              not any(
                  f.rule_id == "generic-assigned-secret"
                  for fl in r1.findings.values() for f in fl),
              "entropy alone must not produce findings")
        oversize = [u for u in r1.unscanned if u["relpath"] == "huge.log"]
        check("6 oversize file explicitly unscanned",
              bool(oversize) and oversize[0]["reason_code"] == "file_too_large")

        # Evolve the snapshot.
        shutil.move(str(repo / "src" / "app.py"), str(repo / "app_moved.py"))
        (repo / "src" / "removeme.py").unlink()  # path vanishes -> uncertain
        r2 = svc.run_scan(repo, audit.RequestContext.create("verify-2", "ci"))
        moved_ghp = [v for fl in r2.findings.values() for v in fl
                     if v.fingerprint == fingerprinter.fingerprint(GHP)]
        check("3 moved same-content candidate classified moved",
              len(moved_ghp) == 1 and moved_ghp[0].state == "moved"
              and moved_ghp[0].occurrences[0]["relpath"] == "app_moved.py",
              str([(v.rule_id, v.state) for v in moved_ghp]))
        third_views = [v for fl in r2.findings.values() for v in fl
                       if v.fingerprint == fingerprinter.fingerprint(THIRD)]
        check("4 deleted-path candidate uncertain_removal, never fixed",
              len(third_views) == 1
              and third_views[0].state == "uncertain_removal"
              and third_views[0].resolution["reason_code"] == "old_path_gone",
              str([(v.rule_id, v.state) for v in third_views]))

        # Replace the slack token in place (same path, changed content).
        (repo / "app_moved.py").write_text(
            "# rotated\nSLACK_BOT_TOKEN = \"renewed-clean-value\"\n",
            encoding="utf-8")
        # Also put the GitHub token back at its old path so it is open,
        # isolating the slack lifecycle check.
        (repo / "src" / "app.py").write_text(f'GITHUB_TOKEN = "{GHP}"\n')
        r3 = svc.run_scan(repo, audit.RequestContext.create("verify-3", "ci"))
        s3 = states(r3)
        check("5 replaced-in-place candidate known_fixed",
              s3.get("slack-bot-token") == "known_fixed", str(s3))

        # 7: no raw secret in report / db / log.
        report_blob = r3.to_json()
        for secret in (GHP, SLACK, THIRD):
            check(f"7 raw secret absent from report ({secret[:8]}…)",
                  secret not in report_blob)
        db_conn = sqlite3.connect(db)
        all_db_text = "\n".join(
            str(tuple(row))
            for table in ("scans", "findings", "occurrences",
                          "file_inventory", "audit_events")
            for row in db_conn.execute(f"SELECT * FROM {table}"))
        for secret in (GHP, SLACK, THIRD):
            check(f"7 raw secret absent from SQLite ({secret[:8]}…)",
                  secret not in all_db_text)
        log_text = log_file.read_text()
        for secret in (GHP, SLACK, THIRD):
            check(f"7 raw secret absent from log ({secret[:8]}…)",
                  secret not in log_text)

        # 8: log identity + structure.
        for line in log_text.splitlines():
            event = json.loads(line)
            assert event["request_id"].startswith("verify-")
            assert event["actor_id"] == "ci"
        check("8 every audit log line is JSON with request id and actor",
              all(json.loads(l)["request_id"].startswith("verify-")
                  for l in log_text.splitlines()))

        conn.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    print("Boundary — intentionally NOT checked (offline by design):")
    print("  * no credential is validated against any live service")
    print("  * no network requests are made; a 'candidate' is never a "
          "confirmed leak")
    print("  * findings in encrypted/obfuscated or bespoke encodings may be "
          "missed")
    print(f"\n{failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
