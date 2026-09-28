"""Lifecycle classification across scans and SQLite state isolation."""

import pytest

from conftest import GHP_TOKEN, SLACK_TOKEN
from secretscan import audit, service as service_mod, state
from secretscan.service import (STATE_KNOWN_FIXED, STATE_MOVED, STATE_NEW,
                                STATE_OPEN, STATE_UNCERTAIN_REMOVAL)

# Distinct fake high-entropy token for the deleted-candidate scenario.
THIRD_TOKEN = "ghp_ogpZeYrOl8JQ5SzSWCAjDBW4sxXq7PLIhG0a"


def _scan(svc, root, label):
    return svc.run_scan(root, audit.RequestContext.create(label, "tester"))


def _states(report):
    return {f.rule_id: f.state
            for fl in report.findings.values() for f in fl}


def test_first_scan_marks_everything_new(service_factory, snapshot_factory):
    root = snapshot_factory({
        "a.py": f'TOKEN = "{GHP_TOKEN}"\n',
        "b.py": f'SLACK_BOT_TOKEN = "{SLACK_TOKEN}"\n',
    })
    report = _scan(service_factory(), root, "req-1")
    assert report.counts["findings_new"] == 2
    assert set(_states(report).values()) == {STATE_NEW}
    assert report.request_id == "req-1"
    assert report.actor_id == "tester"


def test_second_unchanged_scan_marks_open(service_factory, snapshot_factory):
    root = snapshot_factory({"a.py": f'TOKEN = "{GHP_TOKEN}"\n'})
    svc = service_factory()
    _scan(svc, root, "r1")
    report2 = _scan(svc, root, "r2")
    assert _states(report2)["github-classic-pat"] == STATE_OPEN
    assert report2.counts["findings_new"] == 0
    assert report2.counts["findings_open"] == 1


def test_moved_file_classified_moved(service_factory, snapshot_factory):
    root = snapshot_factory({"dir/a.py": f'TOKEN = "{GHP_TOKEN}"\n'})
    svc = service_factory()
    _scan(svc, root, "r1")
    import shutil
    (root / "dir").mkdir(exist_ok=True)
    shutil.move(str(root / "dir" / "a.py"), str(root / "dir" / "a_moved.py"))
    report2 = _scan(svc, root, "r2")
    states = _states(report2)
    assert states["github-classic-pat"] == STATE_MOVED
    moved = [f for fl in report2.findings.values() for f in fl][0]
    assert moved.occurrences[0]["relpath"] == "dir/a_moved.py"


def test_replaced_content_classified_known_fixed(
        service_factory, snapshot_factory):
    root = snapshot_factory({"b.py": f'SLACK_BOT_TOKEN = "{SLACK_TOKEN}"\n'})
    svc = service_factory()
    _scan(svc, root, "r1")
    (root / "b.py").write_text(
        '# rotated\nSLACK_BOT_TOKEN = "renewed-and-clean-no-token-here"\n',
        encoding="utf-8")
    report2 = _scan(svc, root, "r2")
    assert _states(report2)["slack-bot-token"] == STATE_KNOWN_FIXED
    view = next(f for fl in report2.findings.values() for f in fl)
    assert view.resolution["reason_code"] == "old_file_content_changed"


def test_deleted_file_is_uncertain_not_fixed(service_factory, snapshot_factory):
    root = snapshot_factory({"c.py": f'TOKEN = "{THIRD_TOKEN}"\n'})
    svc = service_factory()
    _scan(svc, root, "r1")
    (root / "c.py").unlink()
    report2 = _scan(svc, root, "r2")
    assert _states(report2)["github-classic-pat"] == STATE_UNCERTAIN_REMOVAL
    view = next(f for fl in report2.findings.values() for f in fl)
    assert view.resolution["reason_code"] == "old_path_gone"
    # And it appears in the dedicated uncertainty section, never as "passed".
    assert report2.uncertainties[0]["resolution"]["reason_code"] == \
        "old_path_gone"


def test_newly_ignored_path_is_uncertain(
        tmp_path, rule_pack, fingerprinter, quiet_logger, db_conn,
        snapshot_factory):
    """If the old path becomes scope-ignored, that isn't evidence of a fix."""
    scope_v1 = tmp_path / "scope1.toml"
    scope_v2 = tmp_path / "scope2.toml"
    scope_v1.write_text(
        '[meta]\nname="s"\nversion="v1"\n[limits]\nmax_file_bytes=1048576\n'
        'binary_min_run=8\n[ignore]\npatterns=[".git/"]\n')
    scope_v2.write_text(
        '[meta]\nname="s"\nversion="v2"\n[limits]\nmax_file_bytes=1048576\n'
        'binary_min_run=8\n[ignore]\npatterns=[".git/", "c.py"]\n')
    from secretscan.config import load_scope_pack
    sp1 = load_scope_pack(scope_v1)
    sp2 = load_scope_pack(scope_v2)

    root = snapshot_factory({"c.py": f'TOKEN = "{THIRD_TOKEN}"\n'})
    svc1 = service_mod.ScanService(db_conn, rule_pack, sp1, fingerprinter,
                                   quiet_logger)
    _scan(svc1, root, "r1")
    svc2 = service_mod.ScanService(db_conn, rule_pack, sp2, fingerprinter,
                                   quiet_logger)
    report2 = _scan(svc2, root, "r2")
    assert _states(report2)["github-classic-pat"] == STATE_UNCERTAIN_REMOVAL
    view = next(f for fl in report2.findings.values() for f in fl)
    assert view.resolution["reason_code"] == "old_path_now_ignored"
    assert report2.versions["scope_pack"].startswith("scope:v2@")


def test_state_persists_between_connections(
        tmp_path, rule_pack, scope_pack, fingerprinter, quiet_logger,
        snapshot_factory):
    db = tmp_path / "ws.db"
    root = snapshot_factory({"a.py": f'TOKEN = "{GHP_TOKEN}"\n'})
    conn1 = state.connect(db)
    svc1 = service_mod.ScanService(conn1, rule_pack, scope_pack,
                                   fingerprinter, quiet_logger)
    r1 = _scan(svc1, root, "r1")
    assert r1.scan_id == 1
    conn1.close()

    # Reopen the SAME database: prior finding must be remembered as "open".
    conn2 = state.connect(db)
    svc2 = service_mod.ScanService(conn2, rule_pack, scope_pack,
                                   fingerprinter, quiet_logger)
    r2 = _scan(svc2, root, "r2")
    assert r2.scan_id == 2
    assert _states(r2)["github-classic-pat"] == STATE_OPEN
    conn2.close()


def test_workspaces_are_isolated(
        tmp_path, rule_pack, scope_pack, fingerprinter, quiet_logger,
        snapshot_factory):
    root_a = snapshot_factory({"a.py": f'TOKEN = "{GHP_TOKEN}"\n'})
    # Second independent snapshot tree under a different tmp root.
    root_b_dir = tmp_path / "other"
    root_b_dir.mkdir()
    (root_b_dir / "b.py").write_text(f'TOKEN = "{SLACK_TOKEN}"\n')
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    conn_a = state.connect(db_a)
    conn_b = state.connect(db_b)
    svc_a = service_mod.ScanService(conn_a, rule_pack, scope_pack,
                                    fingerprinter, quiet_logger)
    svc_b = service_mod.ScanService(conn_b, rule_pack, scope_pack,
                                    fingerprinter, quiet_logger)
    _scan(svc_a, root_a, "a1")
    _scan(svc_b, root_b_dir, "b1")
    conn_a.close()
    conn_b.close()
    # Workspace A must not contain B's finding and vice versa.
    check_a = state.connect(db_a)
    check_b = state.connect(db_b)
    rows_a = check_a.execute("SELECT mask FROM findings").fetchall()
    rows_b = check_b.execute("SELECT mask FROM findings").fetchall()
    assert all("xoxb" not in r["mask"] for r in rows_a)
    assert all("ghp_" not in r["mask"] for r in rows_b)
    assert len(rows_a) == 1 and len(rows_b) == 1
    check_a.close()
    check_b.close()


def test_database_never_stores_raw_secret(
        db_conn, service_factory, snapshot_factory):
    root = snapshot_factory({"a.py": f'TOKEN = "{GHP_TOKEN}"\n'})
    svc = service_factory()
    _scan(svc, root, "r1")
    # Grep every stored text column for the full secret.
    for table in ("scans", "findings", "occurrences", "file_inventory",
                  "audit_events"):
        rows = db_conn.execute(f"SELECT * FROM {table}").fetchall()
        blob = repr([dict(r) for r in rows])
        assert GHP_TOKEN not in blob, f"raw secret leaked into {table}"
    # The mask and fingerprint ARE stored.
    f = db_conn.execute("SELECT mask, fingerprint FROM findings").fetchone()
    assert "ghp_" in f["mask"] and "*" in f["mask"]
    assert len(f["fingerprint"]) == 64


def test_unknown_scan_version_database_is_rejected(tmp_path):
    db = tmp_path / "future.db"
    conn = state.connect(db)
    conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(state.StateError, match="schema"):
        state.connect(db)
