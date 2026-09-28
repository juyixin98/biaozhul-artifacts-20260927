"""pytest 公共夹具：每个测试使用独立临时存储目录，互不污染。

同时配置测试日志：每个 pytest 进程一个 run_id，写入 logs/tests.log，
日志行带 run_id 与测试名，业务内核的步骤/判定依据同样落到该文件。
"""
from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
LOG_DIR.mkdir(exist_ok=True)
TEST_RUN_ID = f"tests_{uuid.uuid4().hex[:12]}"


def pytest_configure(config):
    handler = logging.FileHandler(LOG_DIR / "tests.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        f"%(asctime)s %(levelname)-5s run_id={TEST_RUN_ID} %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    # 业务日志器 propagate=False，必须显式挂同一文件 handler，
    # 否则内核步骤/判定依据进不了测试日志
    logging.getLogger("table_merge").addHandler(handler)
    logging.getLogger("table_merge").setLevel(logging.DEBUG)

    from table_merge.logging_setup import set_run_id
    set_run_id(TEST_RUN_ID)


def pytest_runtest_setup(item):
    logging.getLogger("table_merge").info("event=test_start test=%s", item.nodeid)


from table_merge.config import AppConfig  # noqa: E402
from table_merge.service import MergeService  # noqa: E402
from table_merge.storage import MetadataStore  # noqa: E402


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        storage_root=tmp_path / "store",
        host="127.0.0.1", port=0,
        strict_schema=True, max_conflicts=100_000,
        log_level="WARNING", log_file=None,
    )


@pytest.fixture
def store(config: AppConfig) -> MetadataStore:
    return MetadataStore(config.db_path, config.snapshot_dir)


@pytest.fixture
def service(store: MetadataStore) -> MergeService:
    return MergeService(store)


EMP_SCHEMA = {
    "table": "employees",
    "columns": [
        {"name": "id", "type": "int64"},
        {"name": "name", "type": "string"},
        {"name": "city", "type": "string"},
        {"name": "score", "type": "float64"},
        {"name": "active", "type": "bool"},
    ],
    "primary_key": ["id"],
}
EMP_COLUMNS = ("id", "name", "city", "score", "active")


@pytest.fixture
def emp_schema() -> dict:
    return EMP_SCHEMA


def row(id_, name, city, score, active=True) -> dict:
    return {"id": id_, "name": name, "city": city,
            "score": None if score is None else float(score), "active": active}
