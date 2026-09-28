"""Report boundary semantics: failures and uncertainty are separated."""

from __future__ import annotations

import json

from secretscan.report import build_report, render_markdown


def _result_with_all_kinds():
    return {
        "scan_id": "scan_x", "project_id": "proj_x", "root": "/tmp/x",
        "rules_version": "2026.09.01", "classification_version": "1.0.0",
        "config_digest": "d" * 64,
        "started_at": "t0", "finished_at": "t1", "request_id": "req_x",
        "summary": {"files_scanned": 2, "files_ignored": 0, "files_skipped": 2,
                    "file_errors": 1, "candidates_total": 2, "candidates_new": 1,
                    "candidates_uncertain": 1, "known_fixed": 1},
        "candidates": [
            {"rule_id": "aws_access_key_id", "category": "cloud_credential",
             "confidence": "high", "masked": "AKIA…01", "fingerprint": "f1",
             "entropy": 2.08, "source": "rule", "uncertain": False, "reasons": [],
             "state": "new", "triage": "untriaged",
             "locations": [{"path": "a", "line": 1, "column": 1, "kind": "text",
                            "byte_offset": 0, "byte_end": 20}], "files": ["a"]},
            {"rule_id": "entropy_heuristic", "category": "uncertain_high_entropy",
             "confidence": "low", "masked": "Xq7v…aQ", "fingerprint": "f2",
             "entropy": 5.2, "source": "entropy", "uncertain": True,
             "reasons": ["entropy_only:>=4.5"], "state": "new", "triage": "untriaged",
             "locations": [{"path": "b", "line": 2, "column": 3, "kind": "text",
                            "byte_offset": 4, "byte_end": 43}], "files": ["b"]},
        ],
        "known_fixed": [
            {"rule_id": "github_personal_token", "fingerprint": "f3",
             "masked": "ghp_…yz", "category": "scm_token", "confidence": "high",
             "source": "rule", "uncertain": False, "was_baseline_exempt": False,
             "previous_state": "active", "last_seen_scan_id": "scan_old",
             "fixed_in_scan_id": "scan_x", "triage": "untriaged"}
        ],
        "files": [{"path": "a", "kind": "text", "size_bytes": 10,
                   "sha256": "x", "candidate_hits": 1}],
        "skipped": [
            {"path": "big", "kind": "", "reason": "too_large",
             "detail": "size 2 > limit 1; not opened", "size_bytes": 2},
            {"path": "ln", "kind": "", "reason": "symlink_skipped",
             "detail": "symlink not followed", "size_bytes": None},
        ],
        "ignored": [],
        "errors": [{"path": "noperm", "code": "unreadable",
                    "message": "file could not be read"}],
    }


def test_report_separates_failures_and_uncertainty():
    report = build_report(_result_with_all_kinds())
    assert [u["masked"] for u in report["uncertain_conclusions"]] == ["Xq7v…aQ"]
    reasons = report["uncertain_conclusions"][0]["reasons"]
    assert reasons == ["entropy_only:>=4.5"]

    failures = report["failures"]
    assert [e["path"] for e in failures["file_errors"]] == ["noperm"]
    assert [u["path"] for u in failures["unscanned"]] == ["big"]
    assert failures["symlinks_skipped"] == ["ln"]

    kf = report["known_fixed"][0]
    assert kf["masked"] == "ghp_…yz"
    assert kf["was_baseline_exempt"] is False


def test_report_documents_boundary_semantics():
    report = build_report(_result_with_all_kinds())
    interp = report["interpretation"]
    assert "not confirmed leaks" in interp["candidates_are_not_leaks"]
    assert "NOT proof" in interp["known_fixed_meaning"]
    assert "content" in interp["baseline_binding"]
    assert report["scope"]["rules_version"] == "2026.09.01"


def test_markdown_contains_failure_section_and_mask_not_raw():
    md = render_markdown(build_report(_result_with_all_kinds()))
    assert "UNSCANNED `big`" in md
    assert "ERROR `noperm`" in md
    assert "SYMLINK SKIPPED `ln`" in md
    assert "UNCERTAIN" in md
    assert "Known fixed" in md
    assert "AKIA…01" in md
    assert "AKIAFAKE000000000001" not in md
