"""Shared test helpers."""
from __future__ import annotations

import os
import tempfile

import pytest

from app.config import Settings
from app.demux import AnalysisResult, StreamAnalyzer
from app.jobs.manager import JobManager
from app.jobs.store import JobStore


def analyze(data: bytes, record_id: str = "test-record", **overrides) -> AnalysisResult:
    settings = Settings(**overrides)
    return StreamAnalyzer(settings, record_id=record_id).analyze(data)


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=str(tmp_path / "jobs.db"))


@pytest.fixture
def store(tmp_path):
    db = JobStore(str(tmp_path / "jobs.db"))
    yield db
    db.close()


@pytest.fixture
def manager(settings, store):
    mgr = JobManager(store, settings)
    yield mgr
    mgr.shutdown()


def event_codes(result: AnalysisResult) -> list[str]:
    return result.diagnostics.codes()


def events_of(result: AnalysisResult, code: str) -> list:
    return [e for e in result.diagnostics.events if e.code == code]
