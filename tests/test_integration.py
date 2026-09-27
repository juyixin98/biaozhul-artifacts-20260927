"""End-to-end tests against independently generated fixture streams.

Expected results come from the builder-derived ``*.expected.json`` manifests
(ground truth tracked by construction), never from the analyzer itself.
"""
from __future__ import annotations

from app.core.analyzer import analyze

from .conftest import load_fixture


def _codes(report):
    return [f["code"] for f in report.findings]


def test_baseline_map_and_pes(fixture_dir):
    data, manifest = load_fixture(fixture_dir, "baseline")
    r = analyze(data, "t-baseline")
    assert r.request_id == "t-baseline"
    assert r.verdict == "accepted"
    assert r.parsed_packets == manifest["parsed_packets"]
    # Program map learned purely from PAT/PMT (no fixed-PID shortcut).
    progs = r.to_dict()["programs"]
    assert "768" in progs  # 0x300
    pids = {s["elementary_pid"] for s in progs["768"]["streams"]}
    assert pids == {0x1100, 0x1101}
    # Two video PES + one audio PES, none corrupt.
    all_pes = [p for frags in r.to_dict()["pes"].values() for p in frags]
    assert len(all_pes) == 3
    assert all(not p["corrupt"] for p in all_pes)


def test_duplicate_is_accepted_and_not_double_assembled(fixture_dir):
    data, manifest = load_fixture(fixture_dir, "duplicate")
    r = analyze(data, "t-dup")
    dup = [f for f in r.findings if f.code == "duplicate_packet"]
    assert len(dup) == 1
    assert dup[0].disposition.value == "accepted"
    assert dup[0].packet_index == manifest["expected_ledger"][0][
        "packet_index"]
    # The duplicate must not inflate or corrupt the PES.
    video = r.to_dict()["pes"][str(0x1100)]
    assert len(video) == 1 and not video[0]["corrupt"]
    assert video[0]["bytes"] == manifest["expected_pes"][0]["length"]


def test_missing_packets_give_specific_gap_and_pes_gap(fixture_dir):
    data, manifest = load_fixture(fixture_dir, "gap")
    r = analyze(data, "t-gap")
    gaps = [f for f in r.findings if f.code == "cc_gap"]
    assert len(gaps) == 1
    g = gaps[0]
    assert g.disposition.value == "rejected"
    assert g.details["missing_estimate"] == 3
    assert g.packet_index == manifest["expected_ledger"][0][
        "packet_index"]
    assert g.details["expected_cc"] == 2 and g.details["cc"] == 5
    # The PES straddling the loss is marked corrupt; the next PES is fine.
    video = r.to_dict()["pes"][str(0x1100)]
    assert video[0]["corrupt"] is True
    assert not video[1]["corrupt"]


def test_adaptation_only_and_signaled_discontinuity(fixture_dir):
    data, manifest = load_fixture(fixture_dir, "gap")  # noqa: F841
    data, manifest = load_fixture(fixture_dir, "adaptation_signaled")
    r = analyze(data, "t-adapt")
    sig = [f for f in r.findings if f.code == "signaled_discontinuity"]
    assert len(sig) == 1
    assert sig[0].disposition.value == "accepted"
    assert sig[0].packet_index == manifest["expected_ledger"][0][
        "packet_index"]
    # No unsignaled gap and no adaptation-only mismatch.
    assert not [f for f in r.findings if f.code in
                ("cc_gap", "adaptation_only_cc_mismatch")]
    # Both PES (before and after the discontinuity) are intact.
    video = r.to_dict()["pes"][str(0x1100)]
    assert all(not p["corrupt"] for p in video)


def test_cross_packet_pmt_commits_full_map(fixture_dir):
    data, manifest = load_fixture(fixture_dir, "cross_packet_pmt")
    assert manifest["pmt_section_bytes"] > 183  # really spans packets
    r = analyze(data, "t-cross")
    assert not [f for f in r.findings
                if f.code in ("section_crc_error", "section_malformed")]
    progs = r.to_dict()["programs"]
    streams = progs[str(0x300)]["streams"]
    assert {s["elementary_pid"] for s in streams} >= {0x1100, 0x1101}


def test_bad_crc_blocks_map_and_classifies_pid_unknown(fixture_dir):
    data, manifest = load_fixture(fixture_dir, "bad_crc")
    r = analyze(data, "t-crc")
    assert r.verdict == "rejected"
    assert any(f.code == "section_crc_error" for f in r.findings)
    assert r.programs == {}  # failed PMT must never install a map
    assert any(f.code == "unknown_pid_payload" for f in r.findings)


def test_version_switch_is_atomic(fixture_dir):
    data, manifest = load_fixture(fixture_dir, "version_switch")
    r = analyze(data, "t-ver")
    assert r.pat.version == manifest["pat_version_after"]
    progs = r.to_dict()["programs"]
    streams = progs[str(0x300)]["streams"]
    assert [s["elementary_pid"] for s in streams] == [0x1100]
    switches = [f for f in r.findings
                if f.code == "table_version_switch"]
    assert len(switches) == 2  # PAT + PMT


def test_resync_reports_skipped_bytes_and_resumes(fixture_dir):
    data, manifest = load_fixture(fixture_dir, "resync")
    r = analyze(data, "t-resync")
    assert r.resyncs == 1
    assert r.skipped_bytes == manifest["garbage_bytes"]
    resync = [f for f in r.findings if f.code == "resync_occurred"][0]
    assert resync.details["skipped_bytes"] == manifest["garbage_bytes"]
    # Program map survived the resync; post-resync PES is a fresh fragment.
    assert len(r.programs) == 1
    video = r.to_dict()["pes"][str(0x1100)]
    assert not video[-1]["corrupt"]


def test_empty_input_is_rejected_not_accepted(fixture_dir):
    r = analyze(b"", "t-empty")
    assert r.verdict == "rejected"
    assert r.parsed_packets == 0


def test_findings_carry_packet_and_pid_identifiers(fixture_dir):
    data, _ = load_fixture(fixture_dir, "gap")
    r = analyze(data, "t-ids")
    for f in r.findings:
        # Every finding tied to a stream event carries its packet index;
        # framing findings may legitimately have pid=None.
        if f.code not in ("resync_occurred", "trailing_bytes", "no_sync"):
            assert f.packet_index is not None
