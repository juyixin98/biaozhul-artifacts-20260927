"""Pytest harness: structured JSONL run log linking every test to its inputs,
versions, progress and verdicts. Written to tests/logs/run-*.jsonl per run.

Placed at the repo root so the repo root is importable from tests.
"""
import json
import platform
import sys
import time
import uuid
from pathlib import Path

import pytest

LOG_DIR = Path(__file__).parent / "tests" / "logs"


class RunLogger:
    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid.uuid4().hex[:12]
        self.path = LOG_DIR / f"run-{time.strftime('%Y%m%dT%H%M%S')}-{self.run_id}.jsonl"
        self._seq = 0

    def log(self, record):
        self._seq += 1
        record = {"seq": self._seq, "run_id": self.run_id,
                  "ts": round(time.time(), 3), **record}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _versions():
    import fastapi
    import numpy
    import pydantic
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "numpy": numpy.__version__,
        "fastapi": fastapi.__version__,
        "pydantic": pydantic.VERSION,
        "pytest": pytest.__version__,
    }


class RunLogPlugin:
    def __init__(self):
        self.logger = RunLogger()
        self.total = 0
        self.done = 0

    def pytest_sessionstart(self, session):
        self.logger.log({"type": "session_start", "versions": _versions()})

    def pytest_collection_modifyitems(self, session, config, items):
        self.total = len(items)
        self.logger.log({"type": "collected", "total": self.total,
                         "tests": [i.nodeid for i in items]})

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item, call):
        rep = yield
        if rep.when != "call":
            return rep
        self.done += 1
        record = {
            "type": "test_result",
            "test": item.nodeid,
            "outcome": rep.outcome,
            "duration_s": round(rep.duration, 6),
            "progress": f"{self.done}/{self.total}",
        }
        if rep.failed and rep.longrepr:
            record["failure"] = str(rep.longrepr)[-2000:]
        self.logger.log(record)
        return rep

    def pytest_sessionfinish(self, session, exitstatus):
        self.logger.log({"type": "session_end", "exitstatus": int(exitstatus)})
        print(f"\n[run-log] {self.logger.path}")


def pytest_configure(config):
    config.pluginmanager.register(RunLogPlugin(), "run-log-plugin")


@pytest.fixture()
def rlog(request):
    """Per-test structured logger: record inputs, expected/actual values and
    the basis for each verdict, all keyed to the test node id and run id."""
    plugin = request.config.pluginmanager.get_plugin("run-log-plugin")

    def record(kind, **payload):
        plugin.logger.log({"type": "check", "test": request.node.nodeid,
                           "kind": kind, **payload})
    return record
