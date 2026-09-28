"""pytest 公共夹具与测试运行日志（journal）。

每次 pytest 运行有一个 ``run_id``（环境变量 TRIE_TEST_RUN_ID 可注入，便于 CI
关联），每个测试用例的关键断言步骤写入 JSONL journal：
- 输入是什么（前缀/k/词条操作）；
- 当前版本与进度（第几步、共几步）；
- 计算步骤（访问节点数、剪枝次数、每次剪枝的上界与第 k 名分数）；
- 判定依据与结果（PASS/FAIL + 原因类别）。

断言失败或异常也记录 FAIL，不允许“异常即静默通过”。
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from app.config import Settings
from app.main import create_app
from app.normalizer import NORMALIZER_VERSION, normalize
from app.trie import CompressedTrie, Entry

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


class Journal:
    """把每个测试的输入、计算步骤与判定依据写入 JSONL。"""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self._fh = path.open("w", encoding="utf-8")

    def record(self, nodeid: str, phase: str, payload: dict) -> None:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_id": self.run_id,
            "nodeid": nodeid,
            "normalizer_version": NORMALIZER_VERSION,
            "phase": phase,
            **payload,
        }
        self._fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def _load_corpus() -> list[dict]:
    data = json.loads((FIXTURE_DIR / "corpus_long_prefix.json").read_text(encoding="utf-8"))
    return data["entries"]


@pytest.fixture(scope="session")
def run_id() -> str:
    return os.environ.get("TRIE_TEST_RUN_ID") or f"pytest-{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="session")
def journal(run_id, tmp_path_factory) -> Journal:
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    path = log_dir / f"tests-{run_id}.jsonl"
    j = Journal(path, run_id)
    j.record("session", "start", {"message": "test session start", "python": sys.version.split()[0]})
    yield j
    j.record("session", "end", {"message": "test session end"})
    j.close()
    print(f"\n[run_id={run_id}] journal: {path}")


@pytest.fixture
def raw_corpus() -> list[dict]:
    return [dict(e) for e in _load_corpus()]


@pytest.fixture
def trie_with_corpus(raw_corpus):
    """直接构造的 trie（不经存储），用于算法层测试。"""
    t = CompressedTrie()
    for r in raw_corpus:
        t.upsert(Entry(id=r["id"], surface=r["surface"], key=normalize(r["surface"]), score=r["score"]))
    assert not t.verify_integrity()
    return t, raw_corpus


@pytest.fixture
def client(tmp_path):
    """隔离数据目录的 TestClient（每个测试独立 db）。"""
    from fastapi.testclient import TestClient

    settings = Settings.from_env(
        {
            "TRIE_DATA_DIR": str(tmp_path / "data"),
            "TRIE_LOG_DIR": str(tmp_path / "logs"),
        }
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def log(journal, request):
    """返回记录函数 log(phase, verdict, **payload)：带步骤号写入 journal。

    verdict 取值 GIVEN/WHEN/THEN/PASS/FAIL/ERROR；任何异常分支也必须显式记录，
    不允许用 try/except 吞掉后继续断言成功。
    """
    counter = {"step": 0}

    def _log(phase: str, verdict: str, **payload) -> None:
        counter["step"] += 1
        journal.record(
            request.node.name,
            phase,
            {"step": counter["step"], "verdict": verdict, **payload},
        )

    return _log

