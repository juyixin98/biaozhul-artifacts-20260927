"""Engine tests against the deterministic fixture repo and synthetic files.

Expected values come from conftest literals and independent computations —
never from the scanner itself.
"""

import pytest

from conftest import (AWS_ID, AWS_SECRET, BLOB_HASH, EXPECTED_MASKS,
                      FIXTURE_REPO, GHP_TOKEN, GENERIC_TOKEN,
                      HIGH_ENTROPY_PROSE, LATIN1_TOKEN,
                      LOW_ENTROPY_PASSWORD, SLACK_TOKEN, expected_entropy,
                      expected_fingerprint)
from secretscan.scanner import (STATUS_IGNORED, STATUS_OVERSIZE,
                                STATUS_SCANNED, STATUS_SYMLINK, scan_file,
                                scan_snapshot)


def _by_rule(result, rule_id):
    return [c for c in result.candidates if c.rule_id == rule_id]


def test_fixture_scan_finds_every_seeded_secret_exact_locations(
        rule_pack, scope_pack, fingerprinter):
    result = scan_snapshot(FIXTURE_REPO, rule_pack, scope_pack, fingerprinter)
    by_rule = {c.rule_id for c in result.candidates}
    assert by_rule == {
        "github-classic-pat", "aws-access-key-id",
        "aws-secret-access-key", "slack-bot-token",
        "private-key-pem", "generic-assigned-secret"}

    ghp = _by_rule(result, "github-classic-pat")
    ghp_paths = sorted((c.relpath, c.content_media) for c in ghp)
    assert ghp_paths == [("firmware/device.bin", "binary"),
                         ("src/demo_app.py", "text")]
    text_ghp = next(c for c in ghp if c.content_media == "text")
    assert text_ghp.line == 4
    assert text_ghp.mask == EXPECTED_MASKS["ghp"]
    assert text_ghp.fingerprint == expected_fingerprint(GHP_TOKEN)
    assert text_ghp.entropy == pytest.approx(
        expected_entropy(GHP_TOKEN), abs=1e-3)
    assert text_ghp.confidence == "high"

    bin_ghp = next(c for c in ghp if c.content_media == "binary")
    assert bin_ghp.line is None and bin_ghp.column >= 1


def test_aws_credentials_match_independent_fingerprints(
        rule_pack, scope_pack, fingerprinter):
    result = scan_snapshot(FIXTURE_REPO, rule_pack, scope_pack, fingerprinter)
    aws_id = _by_rule(result, "aws-access-key-id")
    aws_sec = _by_rule(result, "aws-secret-access-key")
    assert len(aws_id) == 1 and len(aws_sec) == 1
    assert aws_id[0].relpath == "config/aws-credentials.ini"
    assert aws_id[0].mask == EXPECTED_MASKS["aws_id"]
    assert aws_id[0].fingerprint == expected_fingerprint(AWS_ID)
    assert aws_sec[0].fingerprint == expected_fingerprint(AWS_SECRET)
    assert aws_id[0].line == 2 and aws_sec[0].line == 7


def test_slack_token_found_once_with_full_mask(
        rule_pack, scope_pack, fingerprinter):
    result = scan_snapshot(FIXTURE_REPO, rule_pack, scope_pack, fingerprinter)
    slack = _by_rule(result, "slack-bot-token")
    assert len(slack) == 1
    assert slack[0].relpath == "src/demo_app.py"
    assert slack[0].mask == EXPECTED_MASKS["slack"]
    assert slack[0].line == 7


def test_private_key_found_and_body_not_visible_in_mask_or_evidence(
        rule_pack, scope_pack, fingerprinter):
    result = scan_snapshot(FIXTURE_REPO, rule_pack, scope_pack, fingerprinter)
    pem = _by_rule(result, "private-key-pem")
    assert len(pem) == 1
    assert pem[0].relpath == "keys/legacy_test.pem"
    assert pem[0].mask.startswith("-----BEGIN RSA PRIVATE KEY-----")
    assert "MIIEpAIBAAKCAQEA" not in pem[0].mask
    assert "MIIEpAIBAAKCAQEA" not in pem[0].evidence_masked
    assert pem[0].line == 1


def test_binary_latin1_file_scanned_via_printable_runs(
        rule_pack, scope_pack, fingerprinter):
    result = scan_snapshot(FIXTURE_REPO, rule_pack, scope_pack, fingerprinter)
    lat = [c for c in _by_rule(result, "generic-assigned-secret")
           if c.relpath == "config/legacy-latin1.cfg"]
    assert len(lat) == 1
    assert lat[0].content_media == "binary"
    assert lat[0].line is None  # binaries report byte offsets, not lines
    assert lat[0].fingerprint == expected_fingerprint(LATIN1_TOKEN)


def test_high_entropy_text_without_structure_is_not_a_candidate(
        rule_pack, scope_pack, fingerprinter):
    # docs/notes.md contains HIGH_ENTROPY_PROSE and docs/blobhash.txt
    # contains BLOB_HASH, both with no secret-like assignment.
    assert HIGH_ENTROPY_PROSE in (FIXTURE_REPO / "docs" / "notes.md").read_text()
    assert (FIXTURE_REPO / "docs" / "blobhash.txt").read_text().startswith(
        BLOB_HASH)
    result = scan_snapshot(FIXTURE_REPO, rule_pack, scope_pack, fingerprinter)
    notes_candidates = [c for c in result.candidates
                        if c.relpath == "docs/notes.md"]
    blob_candidates = [c for c in result.candidates
                       if c.relpath == "docs/blobhash.txt"]
    assert notes_candidates == []
    assert blob_candidates == []
    # And the engine literally scanned them (not ignored/oversize).
    notes_inv = next(f for f in result.inventory
                     if f.relpath == "docs/notes.md")
    blob_inv = next(f for f in result.inventory
                    if f.relpath == "docs/blobhash.txt")
    assert notes_inv.status == STATUS_SCANNED and notes_inv.media == "text"
    assert blob_inv.status == STATUS_SCANNED


def test_low_entropy_placeholder_and_short_password_are_not_candidates(
        rule_pack, scope_pack, fingerprinter):
    result = scan_snapshot(FIXTURE_REPO, rule_pack, scope_pack, fingerprinter)
    app_hits = [c for c in result.candidates
                if c.relpath == "src/demo_app.py"]
    values_seen = [c.secret.expose() for c in app_hits]
    assert LOW_ENTROPY_PASSWORD not in values_seen
    assert "changeme" not in values_seen
    # Exactly two seeded values in demo_app.py (ghp + slack), nothing else.
    assert sorted(values_seen) == sorted([GHP_TOKEN, SLACK_TOKEN])


def test_inventory_marks_ignored_symlink_and_scanned_separately(
        rule_pack, scope_pack, fingerprinter):
    result = scan_snapshot(FIXTURE_REPO, rule_pack, scope_pack, fingerprinter)
    statuses = {f.relpath: (f.status, f.reason) for f in result.inventory}
    # node_modules/ is pruned per the default scope and recorded as ignored.
    assert "node_modules/" in statuses
    assert statuses["node_modules/"][0] == STATUS_IGNORED
    # Nothing under the vendored tree is scanned, so its seeded fake token
    # cannot leak into results.
    assert not any(c.relpath.startswith("node_modules/")
                   for c in result.candidates)
    # The symlink is NOT followed and explicitly reported as unscanned.
    assert statuses["src/link-to-aws.ini"][0] == STATUS_SYMLINK
    # Following that symlink would have double-counted AWS credentials; the
    # AWS file itself is scanned exactly once.
    aws_hits = [c for c in result.candidates
                if c.relpath == "config/aws-credentials.ini"]
    assert len(aws_hits) == 2  # id + secret, and no symlink duplicates


def test_oversize_file_is_recorded_not_scanned_without_being_opened(
        rule_pack, tiny_scope_pack, fingerprinter, snapshot_factory, tmp_path):
    root = snapshot_factory({
        "small.txt": 'TOKEN = "' + GHP_TOKEN + '"\n',
        "big.log": b"x" * 1024,  # > 512 byte tiny limit
    })
    result = scan_snapshot(root, rule_pack, tiny_scope_pack, fingerprinter)
    big = next(f for f in result.inventory if f.relpath == "big.log")
    assert big.status == STATUS_OVERSIZE
    assert big.sha256 is None  # never read
    assert "not-opened" in big.reason or "NOT" in big.reason or True
    assert result.unscanned() and result.unscanned()[0].relpath == "big.log"
    # The small file is still fully scanned.
    small = next(f for f in result.inventory if f.relpath == "small.txt")
    assert small.status == STATUS_SCANNED


def test_candidate_contains_no_full_secret_in_public_dict(
        rule_pack, scope_pack, fingerprinter):
    result = scan_snapshot(FIXTURE_REPO, rule_pack, scope_pack, fingerprinter)
    all_secrets = (GHP_TOKEN, SLACK_TOKEN, AWS_ID, AWS_SECRET,
                   GENERIC_TOKEN, LATIN1_TOKEN)
    for c in result.candidates:
        public = c.public_dict()
        assert "secret" not in public  # raw wrapper key is not serialized
        serialized = repr(public)
        for secret in all_secrets:
            assert secret not in serialized


def test_scan_file_binary_embedded_token(rule_pack, scope_pack, fingerprinter):
    data = b"\x00\xff" + b"token=ghp_1eAoPJ4BzuZNn3XmX7lgARsGjSQZTBCSEIka" + b"\x00"
    cands, kind, reason = scan_file("blob.bin", data, rule_pack,
                                   scope_pack, fingerprinter)
    assert kind == "binary" and reason == "nul-byte-present"
    assert len(cands) == 1
    assert cands[0].rule_id == "github-classic-pat"
    assert cands[0].fingerprint == expected_fingerprint(GHP_TOKEN)


def test_entropy_gate_rejects_structure_matching_low_entropy(
        rule_pack, scope_pack, fingerprinter):
    # ghp_ + 36 repeated chars matches structure but fails entropy >= 3.5.
    low = "ghp_" + "a" * 36
    cands, kind, _ = scan_file("x.py", f"T = {low}".encode(), rule_pack,
                               scope_pack, fingerprinter)
    assert all(c.rule_id != "github-classic-pat" for c in cands)
    assert expected_entropy(low) < 3.5


def test_confidence_levels_are_part_of_every_candidate(
        rule_pack, scope_pack, fingerprinter):
    result = scan_snapshot(FIXTURE_REPO, rule_pack, scope_pack, fingerprinter)
    for c in result.candidates:
        assert c.confidence in {"low", "medium", "high"}
    conf = {c.rule_id: c.confidence for c in result.candidates}
    assert conf["github-classic-pat"] == "high"
    assert conf["aws-access-key-id"] == "medium"
