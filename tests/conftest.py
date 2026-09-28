"""pytest 公共夹具：每个测试独立临时库 + TestClient；JSONL 运行日志。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api import create_app  # noqa: E402

MASTER_KEY = "test-master-key-synthetic-only-000000000001"
FIXTURES_PATH = ROOT / "fixtures" / "scenarios.json"
LOG_DIR = ROOT / "data" / "test-runs"


@pytest.fixture(scope="session", autouse=True)
def _log_session():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    yield records
    stamp = time.strftime("%Y%m%dT%H%M%S")
    path = LOG_DIR / f"pytest-{stamp}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n[test-log] 可重放日志: {path}")


@pytest.fixture
def log_record(_log_session, request):
    """记录单条可重放信息：测试编号、关键状态、判断理由。"""
    counter = {"n": 0}

    def _emit(phase: str, **payload):
        counter["n"] += 1
        rec = {"test": request.node.nodeid, "phase": phase, "seq": counter["n"], **payload}
        _log_session.append(rec)
        return rec

    return _emit


@pytest.fixture
def scenarios():
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def make_policy(scenarios):
    def _make(key: str):
        return dict(scenarios["policies"][key])
    return _make


@pytest.fixture
def scenario(scenarios):
    def _find(sid: str):
        return next(s for s in scenarios["scenarios"] if s["id"] == sid)
    return _find


@pytest.fixture
def storage(tmp_path):
    from app.storage import AuditStorage
    db = AuditStorage(tmp_path / "test.sqlite3", MASTER_KEY)
    yield db
    db.close()


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "api-test.sqlite3", MASTER_KEY)
    with TestClient(app) as c:
        yield c
    app.state.storage.close()
