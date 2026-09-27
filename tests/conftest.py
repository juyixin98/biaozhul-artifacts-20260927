"""pytest 共享夹具：

- engine：指向 tmp 路径的 SQLite + 诊断日志（每个测试会话独立 run 目录）；
- fixtures_docs：直接从 fixtures/documents.json 读取，供参考求值器独立判真值；
- 每个测试用例的判定结果写入 logs/runs/<RUN_TAG>/pytest-events.jsonl，
  记录 nodeid、输入/随机种子、成败，日志可关联运行身份。
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from searchdsl import __version__
from searchdsl.config import load_settings
from searchdsl.diagnostics import Diagnostics
from searchdsl.engine import Engine
from searchdsl.evaluator import ReferenceEvaluator
from searchdsl.index import Index
from searchdsl.store import Store

FIXTURES = REPO_ROOT / "fixtures" / "documents.json"
RUN_TAG = os.environ.get("TEST_RUN_TAG") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
RUN_DIR = REPO_ROOT / "logs" / "runs" / RUN_TAG


def _event_file() -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    return RUN_DIR / "pytest-events.jsonl"


def _record(nodeid: str, outcome: str, extra: dict | None = None) -> None:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "run_tag": RUN_TAG,
        "searchdsl_version": __version__,
        "nodeid": nodeid,
        "outcome": outcome,
    }
    if extra:
        rec.update(extra)
    with open(_event_file(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


@pytest.fixture(scope="session")
def run_dir() -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    return RUN_DIR


@pytest.fixture(scope="session")
def fixtures_docs() -> list[dict]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


@pytest.fixture()
def engine(run_dir, fixtures_docs, request) -> Engine:
    db_path = run_dir / f"{request.node.name.replace('/', '_')}.db"
    diag_path = run_dir / "engine-events.jsonl"
    settings = load_settings(database_path_override=db_path, log_path_override=diag_path)
    store = Store(db_path)
    index = Index(store, settings.schema)
    index.rebuild(fixtures_docs)
    eng = Engine(settings, store, index, Diagnostics(diag_path))
    yield eng
    store.close()


@pytest.fixture()
def reference(fixtures_docs, engine) -> ReferenceEvaluator:
    return ReferenceEvaluator(fixtures_docs, engine.settings.schema)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        _record(
            item.nodeid,
            "passed" if report.passed else "failed",
            {"duration_ms": round(report.duration * 1000, 1)},
        )


def pytest_configure(config):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    _record("session", "start", {"pid": os.getpid(), "cwd": str(REPO_ROOT)})


def pytest_sessionfinish(session, exitstatus):
    _record("session", "finish", {
        "exitstatus": exitstatus,
        "tests_run": len([item for item in session.items]),
        "ts_finish": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    })
    print(f"\n[run identity] TEST_RUN_TAG={RUN_TAG}  日志目录: {RUN_DIR}", file=sys.stderr)
