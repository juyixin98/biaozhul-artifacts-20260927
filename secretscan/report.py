"""Explainable reports.

A report is strictly safe to share:
  * candidates carry masks + fingerprints + locations, never raw secrets;
  * uncertain conclusions are a separate top-level section;
  * unscanned / unreadable files and their failure codes are listed
    separately, never silently dropped;
  * rule + classification versions and the config content digest are shown,
    so every finding is traceable.
"""

from __future__ import annotations

from .models import (
    REASON_SYMLINK_SKIPPED,
    REASON_TOO_LARGE,
    REASON_UNREADABLE,
    STATE_BASELINE_EXEMPT,
    STATE_KNOWN_FIXED,
    STATE_NEW,
    STATE_REINTRODUCED,
)


def build_report(result: dict) -> dict:
    candidates = result["candidates"]
    return {
        "report_version": "1.0",
        "scope": {
            "root": result["root"],
            "project_id": result["project_id"],
            "scan_id": result["scan_id"],
            "request_id": result["request_id"],
            "started_at": result["started_at"],
            "finished_at": result["finished_at"],
            "rules_version": result["rules_version"],
            "classification_version": result["classification_version"],
            "config_digest": result["config_digest"],
        },
        "interpretation": {
            "candidates_are_not_leaks": (
                "Candidates are pattern/entropy matches only; they are not "
                "confirmed leaks. This tool runs fully offline and never "
                "validates credentials against a network."
            ),
            "known_fixed_meaning": (
                "'known_fixed' means the content was observed in an earlier scan "
                "and is absent now. It is NOT proof that the credential was "
                "revoked or the issue remediated."
            ),
            "baseline_binding": (
                "Baseline exemptions bind to an HMAC fingerprint of the matched "
                "content plus the rule id; file moves/renames do not change them."
            ),
        },
        "summary": result["summary"],
        "candidates_new": [
            c for c in candidates
            if c["state"] in (STATE_NEW, STATE_REINTRODUCED)
        ],
        "candidates_active_or_exempt": [
            c for c in candidates
            if c["state"] in ("active", STATE_BASELINE_EXEMPT)
        ],
        "uncertain_conclusions": [
            {
                "rule_id": c["rule_id"],
                "masked": c["masked"],
                "fingerprint": c["fingerprint"],
                "confidence": c["confidence"],
                "reasons": c["reasons"],
                "locations": c["locations"],
                "state": c["state"],
            }
            for c in candidates if c["uncertain"]
        ],
        "known_fixed": result.get("known_fixed", []),
        "failures": {
            "file_errors": result["errors"],
            "unscanned": [
                {
                    "path": s["path"],
                    "reason": s["reason"],
                    "detail": s["detail"],
                    "size_bytes": s["size_bytes"],
                }
                for s in result["skipped"]
                if s["reason"] in (REASON_TOO_LARGE, REASON_UNREADABLE)
            ],
            "symlinks_skipped": [
                s["path"] for s in result["skipped"]
                if s["reason"] == REASON_SYMLINK_SKIPPED
            ],
        },
        "coverage": {
            "files_scanned": [f["path"] for f in result["files"]],
            "files_ignored": result["ignored"],
            "ignored_patterns_version": result["rules_version"],
        },
    }


def render_markdown(report: dict) -> str:
    s = report["scope"]
    lines = [
        "# Secret candidate scan report",
        "",
        f"- scan_id: `{s['scan_id']}`",
        f"- request_id: `{s['request_id']}`",
        f"- project_id: `{s['project_id']}`",
        f"- root: `{s['root']}`",
        f"- rules_version: **{s['rules_version']}**",
        f"- classification_version: {s['classification_version']}",
        f"- config_digest: `{s['config_digest'][:16]}…`",
        f"- started: {s['started_at']}",
        f"- finished: {s['finished_at']}",
        "",
        "> Candidates are pattern/entropy matches only; they are not confirmed "
        "leaks. No network validation is ever performed.",
        "> `known_fixed` means absent vs. an earlier scan, not proof of revocation.",
        "",
        "## Summary",
        "",
    ]
    for k, v in report["summary"].items():
        lines.append(f"- {k}: **{v}**")
    lines.append("")

    def render_candidate(c: dict, heading: str) -> list[str]:
        out = ["### " + heading, ""]
        out.append(
            f"- rule: `{c['rule_id']}` ({c['category']}, confidence={c['confidence']}, "
            f"source={c['source']}, state={c['state']})"
        )
        out.append(f"- masked: `{c['masked']}`")
        out.append(f"- fingerprint: `{c['fingerprint']}`")
        if c.get("uncertain"):
            out.append(f"- UNCERTAIN: {', '.join(c['reasons'])}")
        out.append("- locations:")
        for loc in c["locations"]:
            out.append(
                f"  - `{loc['path']}` {loc['kind']} "
                f"line {loc['line']} col {loc['column']} "
                f"(bytes {loc['byte_offset']}..{loc['byte_end']})"
            )
        out.append("")
        return out

    for c in report["candidates_new"]:
        lines += render_candidate(c, f"New / reintroduced: {c['masked']}")
    for c in report["candidates_active_or_exempt"]:
        lines += render_candidate(c, f"{c['state']}: {c['masked']}")

    if report["uncertain_conclusions"]:
        lines += ["## Uncertain conclusions (need human review)", ""]
        for u in report["uncertain_conclusions"]:
            lines.append(
                f"- `{u['rule_id']}` `{u['masked']}` — {', '.join(u['reasons'])}"
            )
        lines.append("")

    if report["known_fixed"]:
        lines += ["## Known fixed (absent vs. earlier scans)", ""]
        for kf in report["known_fixed"]:
            lines.append(
                f"- `{kf['rule_id']}` `{kf['masked']}` last seen in "
                f"`{kf['last_seen_scan_id']}`; baseline_exempt_before={kf['was_baseline_exempt']}"
            )
        lines.append("")

    lines += ["## Failures and unscanned content", ""]
    f = report["failures"]
    if not f["file_errors"] and not f["unscanned"] and not f["symlinks_skipped"]:
        lines.append("- none")
    for e in f["file_errors"]:
        lines.append(f"- ERROR `{e['path']}`: [{e['code']}] {e['message']}")
    for u in f["unscanned"]:
        lines.append(f"- UNSCANNED `{u['path']}`: [{u['reason']}] {u['detail']}")
    for p in f["symlinks_skipped"]:
        lines.append(f"- SYMLINK SKIPPED `{p}`")
    lines.append("")
    return "\n".join(lines)
