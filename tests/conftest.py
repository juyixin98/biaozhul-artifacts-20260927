"""Pytest configuration: structured, run-correlated diagnostic logging.

A fresh run id is generated per test invocation and written into every JSON log
line and into ``artifacts/``. Each test logs the input it is checking, the
computation step and the verdict, so a failure can be traced to a concrete
input/run rather than a generic "interface called".
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
ART.mkdir(exist_ok=True)
RUN_ID = os.environ.get("ABI_TEST_RUN_ID", f"test-{time.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}")
LOG_PATH = ART / f"{RUN_ID}.jsonl"

_logf = open(LOG_PATH, "w", encoding="utf-8")


def log_event(test: str, step: str, verdict: str, **fields) -> None:
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": RUN_ID,
        "test": test,
        "step": step,
        "verdict": verdict,
        **fields,
    }
    _logf.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    _logf.flush()


@pytest.fixture
def log():
    def _emit(step: str, verdict: str = "info", **fields):
        test = os.environ.get("PYTEST_CURRENT_TEST", "?")
        log_event(test.split(" ")[0], step, verdict, **fields)
    return _emit


@pytest.fixture(scope="session")
def run_id() -> str:
    return RUN_ID


def pytest_sessionstart(session):
    log_event("session", "start", "info", python=sys.version.split()[0], argv=sys.argv[:1])


def pytest_runtest_logreport(report):
    if report.when == "call" or (report.when == "setup" and report.outcome == "skipped"):
        verdict = report.outcome
        fields = {}
        if report.failed:
            fields["reason"] = str(getattr(report, "longreprtext", ""))[:800]
        log_event(report.nodeid, report.when, verdict, duration=round(report.duration, 4), **fields)


def pytest_sessionfinish(session, exitstatus):
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    counts = {
        "passed": len(tr.stats.get("passed", [])),
        "failed": len(tr.stats.get("failed", [])),
        "skipped": len(tr.stats.get("skipped", [])),
        "errors": len(tr.stats.get("error", [])),
    }
    summary = {
        "run_id": RUN_ID,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "log": str(LOG_PATH),
        "exitstatus": exitstatus,
        **counts,
    }
    (ART / f"{RUN_ID}.summary.json").write_text(json.dumps(summary, indent=2))
    log_event("session", "finish", "info", **counts, exitstatus=exitstatus)
    _logf.close()
