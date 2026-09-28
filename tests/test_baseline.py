"""Baseline tests: exemptions bind content fingerprints, not file names."""

import pytest

from conftest import GENERIC_TOKEN, expected_fingerprint
from secretscan.baseline import BaselineError, load_baseline


def test_baseline_matches_seeded_fixture_token(fingerprinter, baseline):
    fp = expected_fingerprint(GENERIC_TOKEN)
    entry = baseline.lookup(fp, "generic-assigned-secret")
    assert entry is not None
    assert "legacy demo token" in entry.note


def test_baseline_lookup_is_content_only_rule_filter(fingerprinter, baseline):
    fp = expected_fingerprint(GENERIC_TOKEN)
    # Same content, different rule id -> narrowed entry does not match.
    assert baseline.lookup(fp, "some-other-rule") is None
    # A different content fingerprint does not match the entry.
    other = expected_fingerprint(GENERIC_TOKEN[:-1] + "x")
    assert baseline.lookup(other, "generic-assigned-secret") is None


def test_baseline_rejects_wrong_pepper(tmp_path, fingerprinter):
    other_id = "0" * 12
    bad = tmp_path / "baseline.toml"
    bad.write_text(
        f'[meta]\nversion="1"\npepper_id="{other_id}"\n\n'
        '[[exemptions]]\nfingerprint="abc"\nmask="x"\n')
    with pytest.raises(BaselineError, match="does not match"):
        load_baseline(bad, fingerprinter)


def test_baseline_requires_pepper_id(tmp_path, fingerprinter):
    bad = tmp_path / "baseline.toml"
    bad.write_text('[meta]\nversion="1"\n\n'
                   '[[exemptions]]\nfingerprint="abc"\n')
    with pytest.raises(BaselineError, match="pepper_id"):
        load_baseline(bad, fingerprinter)


def test_baseline_rejects_duplicate_fingerprints(tmp_path, fingerprinter):
    pid = fingerprinter.pepper_id
    bad = tmp_path / "baseline.toml"
    bad.write_text(
        f'[meta]\nversion="1"\npepper_id="{pid}"\n\n'
        '[[exemptions]]\nfingerprint="abc"\nmask="a"\n\n'
        '[[exemptions]]\nfingerprint="abc"\nmask="b"\n')
    with pytest.raises(BaselineError, match="duplicate fingerprint"):
        load_baseline(bad, fingerprinter)


def test_baseline_survives_rename_but_not_content_change(
        service_factory, snapshot_factory, fingerprinter, baseline):
    """The core acceptance rule: exemption follows CONTENT, not the path."""
    line = f'API_TOKEN = "{GENERIC_TOKEN}"\n'
    root = snapshot_factory({"config/service.env": line})
    svc = service_factory(baseline)
    from secretscan import audit
    report1 = svc.run_scan(root, audit.RequestContext.create("r1", "t"))
    states1 = {f.rule_id: f.state
               for fl in report1.findings.values() for f in fl}
    assert states1["generic-assigned-secret"] == "baseline_exempt"

    # Move the file (name and directory both change): exemption must hold.
    import shutil
    (root / "config" / "service.env")
    shutil.move(str(root / "config" / "service.env"),
                str(root / "renamed-elsewhere.env"))
    report2 = svc.run_scan(root, audit.RequestContext.create("r2", "t"))
    moved_views = [f for fl in report2.findings.values() for f in fl]
    moved_entry = next(f for f in moved_views
                       if f.rule_id == "generic-assigned-secret")
    assert moved_entry.state == "baseline_exempt"
    assert moved_entry.occurrences[0]["relpath"] == "renamed-elsewhere.env"

    # Change ONE character of the secret: new fingerprint -> fresh candidate.
    changed = GENERIC_TOKEN[:-1] + ("A" if GENERIC_TOKEN[-1] != "A" else "B")
    (root / "renamed-elsewhere.env").write_text(
        f'API_TOKEN = "{changed}"\n', encoding="utf-8")
    report3 = svc.run_scan(root, audit.RequestContext.create("r3", "t"))
    fresh = [f for fl in report3.findings.values() for f in fl
             if f.rule_id == "generic-assigned-secret"]
    assert len(fresh) == 1
    assert fresh[0].state == "new"
    assert fresh[0].fingerprint != expected_fingerprint(GENERIC_TOKEN)
