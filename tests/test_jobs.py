"""Tests for the SQLite job-state layer and explicit error status handling."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.jobs import JobService, JobStore

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture()
def service(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    settings = Settings(db_path=str(tmp_path / "jobs.db"))
    svc = JobService(store, settings)
    yield store, svc, settings
    store.close()


def test_job_terminal_states_are_explicit(service, tmp_path):
    store, svc, settings = service
    jid_ok = svc.submit((FIX / "clean.srt").read_bytes(), None)
    jid_bad = svc.submit((FIX / "bad_timestamp.srt").read_bytes(), None)

    # Genuinely infeasible: two overlapping 3s cues inside a 5s segment with a
    # 300ms per-cue cap.
    tight_store = JobStore(str(tmp_path / "tight.db"))
    tight_settings = Settings(db_path=str(tmp_path / "tight.db"),
                              segment_boundaries_ms=(5_000, 60_000),
                              max_per_cue_shift_ms=300)
    tight_svc = JobService(tight_store, tight_settings)
    doc = ("1\n00:00:01,000 --> 00:00:04,000\na\n\n"
           "2\n00:00:01,500 --> 00:00:04,500\nb\n")
    jid_inf = tight_svc.submit(doc.encode(), "srt")

    assert store.get(jid_ok)["status"] == "succeeded"
    assert store.get(jid_bad)["status"] == "parse_failed"
    inf_row = tight_store.get(jid_inf)
    assert inf_row["status"] == "failed"
    assert inf_row["result"]["status"] == "infeasible_bounds"
    assert inf_row["result"]["failure_codes"] == ["infeasible_bounds"]
    tight_store.close()


def test_unknown_job_is_none(service):
    store, _, _ = service
    assert store.get("nope") is None


def test_input_hash_and_payload_not_echoed_in_listing(service):
    store, svc, _ = service
    jid = svc.submit(b"1\n00:00:01,000 --> 00:00:02,000\nx\n", "srt")
    row = store.get(jid)
    assert len(row["input_sha256"]) == 64
    assert "input_bytes" not in row
    assert all("input_bytes" not in j for j in store.list())
