"""Indexed storage + offline replay end-to-end (deterministic, failure-preserving)."""
from __future__ import annotations

import pytest

from abibackend.replay import replay
from abibackend.storage import Repository


@pytest.fixture
def repo(tmp_path):
    r = Repository(str(tmp_path / "test.sqlite3"))
    yield r
    r.close()


def test_replay_records_specific_failures(repo, log):
    report = replay(repo, mode="test", run_id="fixed-run-1", chain_id=264, seed=264)
    log("replay", "info", run_id=report.run_id, total=report.total,
        ok=report.succeeded, failed=report.failed)

    assert report.run_id == "fixed-run-1"
    assert report.total == 9
    # Scenario intentionally contains failures; they must be recorded as such.
    assert 0 < report.failed < report.total
    assert report.succeeded + report.failed == report.total

    by_code = {}
    for s in report.steps:
        if not s.ok:
            by_code[s.error_code] = by_code.get(s.error_code, 0) + 1
        else:
            assert s.error_code is None
    log("replay-failures", "info", by_code=by_code)

    # Expected injected categories from the fixture scenario.
    assert "nonce_mismatch" in by_code
    assert "insufficient_balance" in by_code
    assert "bad_signature" in by_code
    assert "invalid_calldata" in by_code

    # Persistence: run row exists and is completed
    row = repo.get_run("fixed-run-1")
    assert row["status"] == "completed"
    assert row["fail_count"] == report.failed
    assert row["tx_count"] == report.total

    txs = repo.list_transactions("fixed-run-1")
    assert len(txs) == report.total
    # No failed tx is mislabeled ok
    assert all(
        (tx["ok"] == 1) == (report.steps[tx["tx_index"]].ok) for tx in txs
    )

    # Accounts snapshot exists and balances are integers-as-strings
    accts = repo.list_accounts()
    assert len(accts) >= 3
    assert repo.last_block()["state_root"] == report.final_state_root


def test_replay_is_deterministic(tmp_path):
    r1 = Repository(str(tmp_path / "a.sqlite3"))
    r2 = Repository(str(tmp_path / "b.sqlite3"))
    rep1 = replay(r1, run_id="d1", chain_id=7, seed=7)
    rep2 = replay(r2, run_id="d2", chain_id=7, seed=7)
    r1.close(); r2.close()
    assert rep1.final_state_root == rep2.final_state_root
    assert [(s.ok, s.error_code) for s in rep1.steps] == [
        (s.ok, s.error_code) for s in rep2.steps
    ]


def test_replay_run_ids_are_queryable(repo):
    replay(repo, run_id="q1")
    runs = repo.list_runs()
    assert any(r["run_id"] == "q1" for r in runs)
