"""pytest 共享夹具：每个测试独立数据目录（状态隔离）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from threshold_service.app import _State, create_app  # noqa: E402
from threshold_service.config import load_settings  # noqa: E402

from tests.oracle_reference import assert_field_self_consistent  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _oracle_verified():
    assert_field_self_consistent()


@pytest.fixture()
def settings(tmp_path):
    return load_settings(
        {"env": "dev", "data_dir": str(tmp_path / "data"),
         "master_key": "00" * 31 + "01"}
    )


@pytest.fixture()
def state(settings):
    return _State(settings)


@pytest.fixture()
def client(state):
    app = create_app(state)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def kernel(state):
    return state.kernel


def issue(kernel, secret: bytes, threshold: int, share_count: int, labels=None):
    return kernel.issue_set(
        secret=secret, threshold=threshold, share_count=share_count,
        labels=labels,
    )


def envelope_to_raw(env: dict) -> str:
    return json.dumps(env, sort_keys=True, separators=(",", ":"))
