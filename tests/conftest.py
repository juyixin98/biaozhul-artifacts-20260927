"""Shared pytest fixtures: per-test replayable run logs and temp workspaces."""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))

from resampler.config import Settings  # noqa: E402
from resampler.observability import RunLogger  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    # Test run logs are kept inside the project (test-logs/) so runs can be
    # replayed/reviewed after the fact; only the database goes to tmp_path.
    persistent_logs = os.path.join(os.path.dirname(__file__), "..", "test-logs")
    return Settings(
        db_path=str(tmp_path / "test.db"),
        log_dir=persistent_logs,
        max_samples_per_chunk=4096,
        max_total_samples=65536,
        max_jobs=8,
        max_ratio_term=1_000_000,
        max_filter_taps=1 << 18,
    )


@pytest.fixture
def runlog(request, settings):
    rid = f"test-{request.node.name}-{os.getpid()}"
    log = RunLogger(settings.log_dir, run_id=rid)

    def check(name, passed, detail, reason):
        log.assertion(name, passed, detail, reason)
        assert passed, f"{name}: {reason} | {detail}"

    log.check = check
    yield log
    rep = getattr(request.node, "_rep_call", None)
    outcome = "failed" if rep is not None and rep.failed else "passed"
    log.summary(outcome)


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def rng():
    return np.random.default_rng(20260928)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call":
        item._rep_call = rep
