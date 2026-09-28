"""pytest 公共夹具与配置。

* 每个测试会话使用独立的临时数据目录（状态隔离）；
* 结构化日志写入文件，run_id 可关联；
* ``load_fixture`` 从 tests/fixtures 读取合成数据。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest

# 测试会话使用独立临时目录与一次性密钥；必须在导入 app.* 之前设置，
# 因为 app.main 在导入时即构建默认 ASGI app（具体测试通过 fixture 注入隔离状态）。
import tempfile

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="anon-tests-"))
os.environ.setdefault("ANON_ALLOW_EPHEMERAL_KEY", "1")
os.environ.setdefault("ANON_DATABASE_PATH", str(_TMP_ROOT / "default.db"))
os.environ.setdefault("ANON_AUDIT_LOG_PATH", str(_TMP_ROOT / "default-audit.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.core.logging_setup import JsonFormatter, configure_logging, get_logger  # noqa: E402
from app.models import DatasetIn  # noqa: E402
from app.state import build_state  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


@pytest.fixture(scope="session", autouse=True)
def _configure_test_logging():
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / "tests.jsonl"
    configure_logging(log_path, level="DEBUG")
    # 同时在 -s 时输出到 stderr
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    get_logger().addHandler(handler)
    get_logger("pytest").info("test session logging configured", extra={"extra_fields": {"event": "session_start", "log_path": str(log_path)}})
    yield


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=str(tmp_path / "state" / "test.db"),
        audit_log_path=str(tmp_path / "audit" / "audit.jsonl"),
        app_log_path=str(tmp_path / "logs" / "app.jsonl"),
        encryption_key="",
        allow_ephemeral_key=True,
        max_rows=1000,
    )


@pytest.fixture()
def state(settings: Settings):
    return build_state(settings)


@pytest.fixture()
def service(state):
    return state.service


def load_fixture(name: str) -> dict:
    path = FIXTURE_DIR / f"{name}.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def make_payload(fixture: dict, k: int, l: int) -> DatasetIn:
    return DatasetIn(
        name=fixture["name"],
        columns=fixture["columns"],
        rows=fixture["rows"],
        k=k,
        l=l,
    )
