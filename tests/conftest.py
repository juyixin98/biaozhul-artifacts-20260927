"""Shared pytest fixtures and run-logging.

Each pytest *run* gets a unique ``run_id`` (UTC timestamp + pid + counter) and
its own directory under ``test-runs/`` containing:

  * ``summary.json``      one line per test: name, outcome, error category,
                          key intermediate state and the judgement reason;
  * ``summary.log``       the same, human readable.

The purpose is postmortem replay: every assertion records the concrete result
and failure *category*, never just "the API was callable".
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest

from lightclient.config import LightClientConfig
from lightclient.fixtures.builder import ChainBuilder
from lightclient.kernel import LightClientKernel
from lightclient.store import Store

RUN_ROOT = Path(__file__).resolve().parent / "test-runs"
_RUN_NS = uuid.uuid4().hex[:8]
RUN_ID = os.environ.get("LC_TEST_RUN_ID") or (
    time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-p{os.getpid()}-{_RUN_NS}"
)
RUN_DIR = RUN_ROOT / RUN_ID

_state: dict = {"records": []}


def _run_dir() -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    return RUN_DIR


def record(
    request: pytest.FixtureRequest,
    outcome: str,
    *,
    category: str | None = None,
    error_code: str | None = None,
    reason: str = "",
    intermediate: dict | None = None,
) -> None:
    rec = {
        "run_id": RUN_ID,
        "test": request.node.nodeid,
        "outcome": outcome,
        "error_category": category,
        "error_code": error_code,
        "reason": reason,
        "intermediate": intermediate or {},
    }
    _state["records"].append(rec)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Automatically record every test call's outcome so the run summary is
    populated even when a test never calls the explicit recorder."""
    if report.when != "call" and not (report.when == "setup" and report.outcome == "skipped"):
        return
    if report.when == "setup" and report.outcome == "skipped":
        outcome, reason = "skipped", str(report.longrepr)[:300]
    elif report.passed:
        outcome, reason = "passed", ""
    elif report.failed:
        outcome = "failed"
        # Keep the assertion message (last line of the failure section).
        reason = ""
        try:
            text = str(report.longrepr)
            reason = text.strip().splitlines()[-1][:300] if text.strip() else ""
        except Exception:
            reason = ""
    else:
        return
    _state["records"].append(
        {
            "run_id": RUN_ID,
            "test": report.nodeid,
            "outcome": outcome,
            "error_category": None,
            "error_code": None,
            "reason": reason,
            "intermediate": {"duration_ms": round(report.duration * 1000, 2)},
        }
    )


@pytest.fixture(scope="session", autouse=True)
def _write_summary():
    yield
    d = _run_dir()
    (d / "summary.json").write_text(
        json.dumps({"run_id": RUN_ID, "records": _state["records"]}, indent=2)
    )
    lines = [f"run_id={RUN_ID}", f"tests={len(_state['records'])}"]
    for r in _state["records"]:
        lines.append(
            f"[{r['outcome']}] {r['test']} "
            f"code={r['error_code']} category={r['error_category']} :: {r['reason']}"
        )
    (d / "summary.log").write_text("\n".join(lines) + "\n")
    # Also keep "latest" pointing at the most recent run for convenience.
    latest = RUN_ROOT / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(d, target_is_directory=True)
    except OSError:
        pass


@pytest.fixture
def run_id() -> str:
    return RUN_ID


@pytest.fixture
def run_dir() -> Path:
    return _run_dir()


@pytest.fixture
def log_case(request, run_dir: Path):
    """Return a recorder that also writes a per-case JSONL event stream."""
    log_path = run_dir / "events.jsonl"

    def _log(event: str, **payload) -> None:
        rec = {"run_id": RUN_ID, "test": request.node.nodeid, "event": event, **payload}
        with log_path.open("a") as fh:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")

    _log("start")
    return _log


@pytest.fixture
def config() -> LightClientConfig:
    return LightClientConfig(trust_period_seconds=3600, quorum_weight=2)


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


@pytest.fixture
def kernel(store: Store, config: LightClientConfig) -> LightClientKernel:
    builder = ChainBuilder()
    _g, env = builder.genesis()
    k = LightClientKernel(
        store, config, builder.checkpoint_pub, run_id=RUN_ID
    )
    k.bootstrap(env)
    return k


@pytest.fixture
def builder() -> ChainBuilder:
    return ChainBuilder()


@pytest.fixture
def bootstrapped(builder, store, config):
    """Return (kernel, builder) with the builder's genesis already trusted."""
    _g, env = builder.genesis()
    k = LightClientKernel(store, config, builder.checkpoint_pub, run_id=RUN_ID)
    k.bootstrap(env)
    return k, builder
