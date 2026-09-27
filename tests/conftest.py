"""pytest 共享设施：夹具、应用、带运行身份的测试日志。

每次测试运行生成 logs/testrun-<run_id>.jsonl：
- 头部记录 run_id、时间、Python 与依赖版本、各夹具 sha256；
- 每个用例记录关键计算值、判定依据（参考来源）与 verdict。
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATED = REPO_ROOT / "fixtures" / "generated"
REFERENCES = REPO_ROOT / "fixtures" / "references"
LOGS = REPO_ROOT / "logs"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "fixtures"))


def _versions() -> dict:
    import fastapi
    import numpy
    import pydantic

    return {
        "python": platform.python_version(),
        "fastapi": fastapi.__version__,
        "numpy": numpy.__version__,
        "pydantic": pydantic.__version__,
        "pytest": pytest.__version__,
    }


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


class RunLogger:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id

    def write(self, record: dict) -> None:
        record = {"run_id": self.run_id, "ts": round(time.time(), 3), **record}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


_RUN_LOGGER: RunLogger | None = None


def pytest_sessionstart(session):
    global _RUN_LOGGER
    LOGS.mkdir(exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    _RUN_LOGGER = RunLogger(LOGS / f"testrun-{run_id}.jsonl", run_id)
    fixture_hashes = {}
    if GENERATED.exists():
        for mp4 in sorted(GENERATED.glob("*.mp4")):
            fixture_hashes[mp4.name] = hashlib.sha256(mp4.read_bytes()).hexdigest()
    _RUN_LOGGER.write(
        {
            "event": "run_header",
            "versions": _versions(),
            "git_commit": _git_commit(),
            "fixture_sha256": fixture_hashes,
        }
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == "call" and _RUN_LOGGER is not None:
        _RUN_LOGGER.write(
            {
                "event": "test_verdict",
                "test": item.nodeid,
                "verdict": report.outcome,
                "duration_s": round(report.duration, 4),
            }
        )
    return report


@pytest.fixture(scope="session")
def run_logger() -> RunLogger:
    assert _RUN_LOGGER is not None
    return _RUN_LOGGER


@pytest.fixture
def testlog(run_logger) -> RunLogger:
    return run_logger


@pytest.fixture(scope="session", autouse=True)
def ensure_fixtures() -> None:
    """夹具缺失时自动构建（构建器独立于被测核心）。"""

    if not (GENERATED / "bframes.mp4").exists():
        import build_fixtures

        build_fixtures.main()


@pytest.fixture(scope="session")
def generated_dir() -> Path:
    return GENERATED


@pytest.fixture(scope="session")
def references() -> dict:
    return {p.stem: json.loads(p.read_text()) for p in sorted(REFERENCES.glob("*.json"))}


@pytest.fixture(scope="session")
def app_settings(tmp_path_factory):
    from mp4timeline.config import Settings

    root = tmp_path_factory.mktemp("mp4tl")
    return Settings(
        fixtures_dir=GENERATED,
        db_path=root / "jobs.sqlite3",
        allowed_roots=(GENERATED, REPO_ROOT / "fixtures"),
    )


@pytest.fixture(scope="session")
def client(app_settings):
    from fastapi.testclient import TestClient
    from mp4timeline.api.app import create_app

    return TestClient(create_app(app_settings))
