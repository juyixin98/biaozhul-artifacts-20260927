"""pytest 配置层：运行身份、日志关联、独立临时数据库与服务夹具。

每条测试日志都带两个关联标识：

- ``run_id``：本次 pytest 运行的唯一身份（命令行可通过 ``--run-id`` 指定，
  否则自动生成），写入响应头与日志行；
- 测试节点名 + 输入摘要：便于把失败用例和具体输入对应起来。
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.api import create_app  # noqa: E402
from app.engine import Engine  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def pytest_addoption(parser):
    parser.addoption(
        "--run-id",
        action="store",
        default=None,
        help="本次测试运行身份（默认自动生成），用于关联日志与失败记录",
    )


@pytest.fixture(scope="session")
def run_id(request) -> str:
    rid = request.config.getoption("--run-id") or f"test-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    log_dir = Path("test-runs")
    log_dir.mkdir(exist_ok=True)
    handler = logging.FileHandler(log_dir / f"{rid}.log", mode="w", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [run=%(run_id)s] %(name)s | %(message)s")
    )

    class _RunFilter(logging.Filter):
        def __init__(self, value: str):
            super().__init__()
            self.value = value

        def filter(self, record):
            record.run_id = self.value
            return True

    handler.addFilter(_RunFilter(rid))
    root = logging.getLogger("ctrie")
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    # 同时输出到控制台（低噪声级别由各用例自行记录关键步骤）
    stream = logging.StreamHandler()
    stream.addFilter(_RunFilter(rid))
    stream.setFormatter(logging.Formatter("[%(levelname)s run=%(run_id)s] %(message)s"))
    stream.setLevel(logging.INFO)
    root.addHandler(stream)
    root.propagate = False

    logging.getLogger("ctrie").info(
        "测试运行开始 run_id=%s python=%s cwd=%s", rid, sys.version.split()[0], os.getcwd()
    )
    return rid


@pytest.fixture()
def engine(tmp_path, run_id):
    db = tmp_path / f"engine-{run_id}.db"
    eng = Engine(str(db), topk_max=50)
    logger = logging.getLogger("ctrie")
    logger.info("fixture: 创建 Engine db=%s", db)
    yield eng
    violations = eng.check_invariants()
    if violations:
        logger.error("fixture: 用例结束后不变量违规: %s", violations)
    assert not violations, f"用例后索引不变量违规: {violations}"


@pytest.fixture()
def client(engine, run_id):
    settings = Settings(db_path=engine.db_path, topk_max=50)
    app = create_app(settings=settings, engine=engine)
    with TestClient(app) as c:
        c.headers.update({"X-Request-ID": f"{run_id}-httpx"})
        yield c


@pytest.fixture()
def log():
    return logging.getLogger("ctrie")
