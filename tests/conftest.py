"""Pytest configuration: per-run replay logs.

Every test run gets a directory ``test-runs/run-<UTCstamp>-<pid>/`` with:

* ``events.jsonl`` -- one JSON record per significant test moment:
  RUN_START, CHECK (each explicit assertion-of-result/failure-class pair, with
  the key intermediate state and the reason for the verdict), and RUN_END;
* ``summary.json`` -- totals and pass/fail/skip lists.

A failing test therefore leaves behind enough to reconstruct *what* was
compared and *why* it was judged wrong, which is the replay contract.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

RUN_ROOT = Path(__file__).resolve().parent.parent / "test-runs"
_SEQ_FILE = RUN_ROOT / "run-seq.txt"


def _run_id() -> tuple[str, int]:
    """Allocate a monotonic run number across runs, return (id, number)."""
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        n = int(_SEQ_FILE.read_text().strip()) + 1
    except (FileNotFoundError, ValueError):
        n = 1
    _SEQ_FILE.write_text(str(n))
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"run-{n:05d}-{stamp}-{os.getpid()}", n


@pytest.fixture(scope="session")
def run_logger():
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    rid, number = _run_id()
    d = RUN_ROOT / rid
    d.mkdir()
    logger = RunLogger(d)
    logger.event(
        "RUN_START",
        run_number=number,
        run_id=rid,
        python_version=sys.version.split()[0],
        cwd=str(Path.cwd()),
    )
    yield logger
    logger.close()


@pytest.fixture(autouse=True)
def _attach_logger(request, run_logger):
    # Make the session logger reachable from the report hook for EVERY test,
    # including ones that do not request run_logger directly.
    request.config._run_logger = run_logger


class RunLogger:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.events = directory / "events.jsonl"
        self.summary = directory / "summary.json"
        self._seq = 0
        self.results: dict[str, str] = {}
        self.run_number = int(directory.name.split("-")[1])

    def event(self, kind: str, **payload) -> None:
        self._seq += 1
        rec = {"seq": self._seq, "ts": time.time(), "event": kind, **payload}
        with self.events.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def check(self, nodeid: str, name: str, expected, actual, passed: bool, reason: str,
              intermediate: dict | None = None) -> None:
        self.results.setdefault(nodeid, "passed")
        if not passed:
            self.results[nodeid] = "failed"
        self.event(
            "CHECK",
            test=nodeid,
            check=name,
            expected=_safe(expected),
            actual=_safe(actual),
            passed=bool(passed),
            reason=reason,
            intermediate=intermediate or {},
        )

    def mark_skip(self, nodeid: str, reason: str) -> None:
        self.results[nodeid] = "skipped"
        self.event("SKIP", test=nodeid, reason=reason)

    def close(self) -> None:
        passed = sorted(k for k, v in self.results.items() if v == "passed")
        failed = sorted(k for k, v in self.results.items() if v == "failed")
        skipped = sorted(k for k, v in self.results.items() if v == "skipped")
        with self.summary.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "run_number": self.run_number,
                    "run_dir": str(self.directory),
                    "passed": len(passed),
                    "failed": len(failed),
                    "skipped": len(skipped),
                    "passed_tests": passed,
                    "failed_tests": failed,
                    "skipped_tests": skipped,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        self.event(
            "RUN_END",
            run_number=self.run_number,
            passed=len(passed),
            failed=len(failed),
            skipped=len(skipped),
            summary=str(self.summary),
        )


def _safe(obj):
    if isinstance(obj, (bytes, bytearray, memoryview)):
        try:
            return bytes(obj).decode("utf-8")
        except UnicodeDecodeError:
            return {"_base64_truncated": True}
    if isinstance(obj, (list, tuple)):
        return [_safe(x) for x in obj][:50]
    if isinstance(obj, dict):
        return {str(k): _safe(v) for k, v in list(obj.items())[:50]}
    s = repr(obj)
    return s if len(s) < 2000 else s[:2000] + "...<truncated>"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    logger = getattr(item.config, "_run_logger", None)
    if logger is None:
        return
    if rep.when == "setup":
        if rep.skipped:
            logger.mark_skip(item.nodeid, str(rep.longrepr or "skipped"))
        else:
            logger.results.setdefault(item.nodeid, "passed")
    if rep.when == "call" and rep.skipped:
        logger.mark_skip(item.nodeid, str(rep.longrepr or "skipped"))
    if rep.when == "call" and rep.passed:
        logger.results.setdefault(item.nodeid, "passed")
    if rep.when == "call" and rep.failed:
        logger.results[item.nodeid] = "failed"
        logger.event(
            "FAILURE",
            test=item.nodeid,
            exception=repr(call.excinfo.value) if call.excinfo else None,
            traceback=str(rep.longrepr) if rep.longrepr else None,
        )
