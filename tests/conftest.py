"""Shared pytest fixtures + the test run log.

Tests assert concrete results and failure *categories*.  Independently of
the service's own log, this conftest maintains a *test* log: one JSON line
per judgement with a run number, the key intermediate state, the oracle's
expected value and the verdict reason.  That file is what scripts/replay.py
replays.

Environment is pointed at throwaway paths BEFORE application imports so no
test can touch real data/ or logs/.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Point the service at isolated paths before it reads configuration.
_TMP_ROOT = Path(__file__).parent / "_run_artifacts"
_TMP_ROOT.mkdir(exist_ok=True)
_RUN_TS = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
_RUN_ID = f"test-{_RUN_TS}-{os.getpid()}"
os.environ["TEXTINDEX_DB"] = str(_TMP_ROOT / f"{_RUN_ID}.db")
os.environ["TEXTINDEX_LOG"] = str(_TMP_ROOT / f"{_RUN_ID}.service.jsonl")
os.environ["TEXTINDEX_MAX_DOC_BYTES"] = str(1 << 16)
os.environ["TEXTINDEX_MAX_CLUSTERS"] = "5000"

# Make src/ importable without installation.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from textindex.config import Settings  # noqa: E402
from textindex.diagnostics import RunLogger  # noqa: E402
from textindex.service import TextIndexService  # noqa: E402
from textindex.storage import Storage  # noqa: E402


TEST_LOG_PATH = _TMP_ROOT / f"{_RUN_ID}.tests.jsonl"
SUMMARY_PATH = _TMP_ROOT / f"{_RUN_ID}.summary.txt"


class TestRunRecorder:
    """Records every test judgement (success and expected failure) to JSONL."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._seq = 0
        self._fh = open(path, "w", encoding="utf-8", buffering=1)

    def judge(
        self,
        *,
        test: str,
        kind: str,
        passed: bool,
        expected: object = None,
        actual: object = None,
        intermediate: dict | None = None,
        reason: str = "",
        error_category: str | None = None,
        error_code: str | None = None,
    ) -> dict:
        with self._lock:
            self._seq += 1
            rec = {
                "run_id": _RUN_ID,
                "run_no": self._seq,
                "ts": datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds"),
                "test": test,
                "kind": kind,
                "verdict": "PASS" if passed else "FAIL",
                "expected": expected,
                "actual": actual,
                "intermediate": intermediate or {},
                "reason": reason,
            }
            if error_category:
                rec["error_category"] = error_category
            if error_code:
                rec["error_code"] = error_code
            self._fh.write(json.dumps(rec, ensure_ascii=False,
                                      default=str) + "\n")
            return rec

    def close(self, counts: dict) -> None:
        self._fh.close()


@pytest.fixture(scope="session")
def run_id() -> str:
    return _RUN_ID


@pytest.fixture(scope="session")
def recorder():
    rec = TestRunRecorder(TEST_LOG_PATH)
    counts = {"PASS": 0, "FAIL": 0}
    yield rec, counts
    rec.close(counts)
    with open(SUMMARY_PATH, "w", encoding="utf-8") as fh:
        fh.write(f"test run {_RUN_ID}\n")
        fh.write(f"log: {TEST_LOG_PATH}\n")
        fh.write(f"PASS={counts['PASS']} FAIL={counts['FAIL']}\n")


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "test.db"),
        log_path=str(tmp_path / "test.service.jsonl"),
        max_document_bytes=1 << 16,
        max_clusters=5000,
    )


@pytest.fixture()
def service(settings):
    storage = Storage(settings.db_path)
    logger = RunLogger(settings.log_path)
    svc = TextIndexService(settings, storage=storage, logger=logger)
    try:
        yield svc
    finally:
        svc.close()
