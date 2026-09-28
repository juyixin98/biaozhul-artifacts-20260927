"""Pytest configuration: run identity, structured progress logging, artifacts.

Every test run gets ``test-runs/<run-id>/`` containing:
  * ``run-info.json``  - versions, cwd, git commit, start time;
  * ``results.jsonl``  - one line per test (setup/call/teardown outcome,
                         duration, assertion context);
  * ``steps.jsonl``    - in-test computation-step records written via the
                         ``step_logger`` fixture;
so a failure can be traced from the assertion back to the exact input and
computation steps, and skipped/not-executed tests remain visible.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pyarrow as pa
import pytest

RUNS_ROOT = Path(__file__).resolve().parent.parent / "test-runs"
RUN_ID = os.environ.get("DICUNIFY_TEST_RUN_ID", f"run-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}")
RUN_DIR = RUNS_ROOT / RUN_ID


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            cwd=Path(__file__).resolve().parent.parent, check=False,
        )
        return out.stdout.strip() or None
    except OSError:
        return None


def _versions() -> dict:
    import fastapi
    import pydantic

    from app import __version__

    return {
        "run_id": RUN_ID,
        "service_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pyarrow": pa.__version__,
        "fastapi": fastapi.__version__,
        "pydantic": pydantic.VERSION,
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "git_commit": _git_commit(),
        "start_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def pytest_configure(config) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(RUN_DIR / "run-info.json", "w") as f:
        json.dump(_versions(), f, indent=2, sort_keys=True)
    config._dicunify_run_dir = RUN_DIR
    print(f"\n[dicunify] test run artifacts: {RUN_DIR}", file=sys.stderr)


def pytest_runtest_logreport(report) -> None:
    if report.when not in ("setup", "call", "teardown"):
        return
    # Only record call outcomes for passing tests; record setup/teardown only
    # when non-passed so skips/errors stay visible.
    if report.when == "call" or report.outcome != "passed":
        record = {
            "nodeid": report.nodeid,
            "phase": report.when,
            "outcome": report.outcome,
            "duration_s": round(report.duration, 6),
            "run_id": RUN_ID,
        }
        if report.outcome == "skipped":
            record["skip_reason"] = report.longreprtext
        if report.outcome == "failed":
            long = str(report.longrepr)
            record["longrepr_tail"] = long[-2000:]
            if report.capstdout:
                record["stdout_tail"] = report.capstdout[-1000:]
        with open(RUN_DIR / "results.jsonl", "a") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")


class StepLogger:
    """Test-side recorder of computation steps and inputs for correlation."""

    def __init__(self, nodeid: str):
        self.nodeid = nodeid

    def step(self, name: str, **data) -> None:
        rec = {"nodeid": self.nodeid, "run_id": RUN_ID,
               "ts": time.time(), "step": name, **data}
        with open(RUN_DIR / "steps.jsonl", "a") as f:
            f.write(json.dumps(rec, sort_keys=True, default=repr) + "\n")


@pytest.fixture
def run_id() -> str:
    return RUN_ID


@pytest.fixture
def run_dir() -> Path:
    return RUN_DIR


@pytest.fixture
def step_logger(request) -> StepLogger:
    return StepLogger(request.node.nodeid)


# ---------------------------------------------------------------------------
# Local synthetic fixtures (all data generated locally, no external inputs).
# ---------------------------------------------------------------------------
import random as _random

from tests.fixtures import make_batch as _make_batch


@pytest.fixture
def rng():
    # Fixed seed for reproducibility; tests may reseed via rng.seed(...).
    return _random.Random(20260928)


@pytest.fixture
def overlapping_batches():
    """Two batches whose same values use *different* local codes, and which
    share a local code for *different* values:

        b0: code 0='a', code 1='b'
        b1: code 0='b', code 1='c', code 2='a'
    Sorted global dict -> ['a'(0), 'b'(1), 'c'(2)].
    Includes NULL rows.
    """
    b0 = _make_batch(
        "b0", ["a", "b"], [0, 1, 0, 1, 0],
        [True, True, False, True, True],
    )
    b1 = _make_batch(
        "b1", ["b", "c", "a"], [0, 1, 2, 0, 2, 1],
        [True, True, True, False, True, False],
    )
    return [b0, b1]


@pytest.fixture
def duplicate_dict_batch():
    # local codes 1 and 3 are duplicates of code 0 ('x'); code 2 is distinct.
    return _make_batch("dup", ["x", "x", "y", "x"], [0, 1, 2, 3, 2, 0])


@pytest.fixture
def empty_dictionary_batch():
    # no values at all; validity makes every row NULL -> indices must be [0..]
    return _make_batch("nulls", [], [0, 0, 0, 0],
                       [False, False, False, False])


@pytest.fixture
def width_batches():
    """Batches spanning the uint8/uint16 boundary with disjoint value sets."""

    def vals(n, prefix):
        return [f"{prefix}{i}" for i in range(n)]

    b0 = _make_batch("w0", vals(200, "a"), list(range(200)))
    b1 = _make_batch("w1", vals(60, "b"), list(range(60)))
    return [b0, b1]  # cardinality 260 -> uint16
