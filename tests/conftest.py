"""Shared fixtures and the replayable run-log recorder.

Every test session gets a unique ``run_id`` (UTC timestamp + short uuid).
Each test can call ``recorder.record(...)`` with key intermediate state and the
reasoning behind assertions; the records are written as JSON Lines to
``tests/_runs/<run_id>/run.log.jsonl`` so any failure can be replayed with the
exact numeric thresholds, shapes and RNG seeds that produced it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import uuid
from pathlib import Path

import numpy as np
import pytest

RUNS_DIR = Path(__file__).parent / "_runs"
_SESSION_RECORDER: "RunRecorder | None" = None


class RunRecorder:
    def __init__(self, run_dir: Path, run_id: str) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.path = run_dir / "run.log.jsonl"
        self._fh = open(self.path, "w", encoding="utf-8")

    def record(self, name: str, phase: str, *, ok: bool | None = None,
               reason: str = "", **state) -> None:
        rec = {
            "run_id": self.run_id,
            "test": name,
            "phase": phase,
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "ok": ok,
            "reason": reason,
            "state": _jsonable(state),
        }
        self._fh.write(json.dumps(rec, sort_keys=True) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return {"__ndarray__": True, "shape": list(obj.shape),
                "dtype": str(obj.dtype),
                "head": [float(v) for v in np.asarray(obj).ravel()[:8]]}
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, (float, int, str, bool)) or obj is None:
        return obj
    return repr(obj)


def _make_run_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
def run_id() -> str:
    rid = os.environ.get("RESAMP_TEST_RUN_ID") or _make_run_id()
    return rid


@pytest.fixture(scope="session")
def recorder(run_id) -> RunRecorder:
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    rec = RunRecorder(run_dir, run_id)
    rec.record("session", "start", reason="pytest session start",
               python=sys.version, numpy=np.__version__)
    yield rec
    rec.record("session", "end", reason="pytest session end")
    rec.close()


@pytest.fixture
def log(request, recorder: RunRecorder):
    """Bind the recorder to the current test name."""
    def _emit(phase: str, *, ok: bool | None = None, reason: str = "", **state):
        recorder.record(request.node.name, phase, ok=ok, reason=reason, **state)
    _emit.path = recorder.path
    _emit.run_id = recorder.run_id
    _emit.run_dir = recorder.run_dir
    return _emit


@pytest.fixture
def rng():
    return np.random.default_rng(20260928)


def _failure_category(report) -> str:
    """Classify a failing report into the project's failure vocabulary."""
    text = str(report.longrepr)
    for name in ("ComputationError", "ResourceExhaustedError",
                 "StateConflictError", "InvalidInputError", "NotFoundError"):
        if name in text:
            return name
    if "AssertionError" in text or "assert " in text:
        return "AssertionError"
    if report.longreprtext:
        return report.longreprtext.splitlines()[-1][:200]
    return "unknown"


def pytest_runtest_logreport(report):
    """Persist a start/pass/fail record for every test under the session's
    run_id, so any failure is replayable from the JSONL alone.  Tests that use
    the ``log`` fixture add numeric intermediate state and rationale on top."""
    rec = _SESSION_RECORDER
    if rec is None or report.when not in ("setup", "call"):
        return
    nodeid = report.nodeid
    if report.when == "setup":
        if report.failed:  # collection/setup errors still need a trail
            rec.record(nodeid, "test_fail", ok=False,
                       reason=_failure_category(report),
                       longrepr=str(report.longrepr)[:4000])
        else:
            rec.record(nodeid, "test_start",
                       reason="deterministic default RNG seed: 20260928",
                       seed=20260928)
    elif report.when == "call":
        rec.record(nodeid,
                   "test_pass" if report.passed else "test_fail",
                   ok=bool(report.passed),
                   reason="" if report.passed else _failure_category(report),
                   duration_s=round(report.duration, 6),
                   longrepr="" if report.passed
                   else str(report.longrepr)[:4000])


@pytest.fixture(scope="session", autouse=True)
def _attach_recorder_to_config(recorder: RunRecorder):
    global _SESSION_RECORDER
    _SESSION_RECORDER = recorder
    yield
    _SESSION_RECORDER = None


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    runs = sorted(RUNS_DIR.glob("*/run.log.jsonl"))
    if runs:
        terminalreporter.write_sep("-", f"run log: {runs[-1]}")
