"""Shared pytest fixtures.

Makes both ``src/`` (the package under test) and ``scripts/`` (the independent
oracle) importable, and provides a log directory so every test run keeps a
replayable JSONL trace with its run id.
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from lc.chain import ChainKernel  # noqa: E402
from lc.clock import FixedClock, RunRecorder  # noqa: E402
from lc.config import KernelConfig  # noqa: E402
from lc.store import Store  # noqa: E402


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return str(d)


@pytest.fixture
def config():
    # Deterministic synthetic trust period: 7 days.
    return KernelConfig(trust_period_ms=7 * 24 * 60 * 60 * 1000)


@pytest.fixture
def recorder(log_dir):
    return RunRecorder(log_dir=log_dir)


@pytest.fixture
def clock():
    # Fixed after the latest golden-vector timestamp (max ~T0 + 310*6s), so
    # no header looks "in the future". Boundary tests move this clock itself.
    return FixedClock(1_700_000_000_000 + 400 * 6_000)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "lc.db"))
    yield s
    s.close()


@pytest.fixture
def kernel(store, clock, config, recorder):
    return ChainKernel(store, clock, config, recorder)


@pytest.fixture
def golden():
    with open(
        os.path.join(ROOT, "tests", "fixtures", "golden.json"), encoding="utf-8"
    ) as fh:
        return json.load(fh)


@pytest.fixture
def oracle():
    import oracle as o

    return o
