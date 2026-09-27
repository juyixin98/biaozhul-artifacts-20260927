"""Pytest configuration: per-session log file correlated with test identity.

Every test's log records are written both to the console and to a per-session
file under ``tests/logs/``. Each test starts with a header naming the test and
its input fixture (``[CASE]``), versions of the core dependencies (``[ENV]``),
and the run/job id is embedded in every pipeline/solver log line so results can
be traced back to the exact input that produced them.
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

import fastapi
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
SESSION_ID = f"pytest-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
LOG_FILE = LOG_DIR / f"{SESSION_ID}.log"

_root = logging.getLogger("subguard")
_root.setLevel(logging.DEBUG)
_fh = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"))
_root.addHandler(_fh)
_sh = logging.StreamHandler(sys.stderr)
_sh.setLevel(logging.INFO)
_sh.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
_root.addHandler(_sh)


def _env_banner() -> str:
    import platform
    import pydantic
    from app import __version__
    import sqlite3
    return (
        f"subguard={__version__} python={platform.python_version()} "
        f"numpy={np.__version__} fastapi={fastapi.__version__} "
        f"pydantic={pydantic.VERSION} sqlite={sqlite3.sqlite_version}"
    )


_root.info("[ENV] session=%s %s pid=%d log_file=%s",
           SESSION_ID, _env_banner(), os.getpid(), LOG_FILE)


@pytest.fixture(autouse=True)
def case_log_banner(request):
    logger = logging.getLogger("subguard.test")
    logger.info("[CASE] BEGIN %s (nodeid=%s)", request.node.name, request.node.nodeid)
    yield
    logger.info("[CASE] END   %s", request.node.name)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    terminalreporter.write_sep("-", f"full trace log: {LOG_FILE}")
