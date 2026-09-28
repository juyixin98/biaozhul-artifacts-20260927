"""Shared pytest configuration: logging identity defaults, versions, temp DB."""
from __future__ import annotations

import logging
import os
import platform
import sys
from pathlib import Path

import pyarrow as pa
import pytest

# Ensure pytest's log formatter never crashes on our contextual attributes.
class _RunFilter(logging.Filter):
    def filter(self, record):
        record.run_id = getattr(record, "run_id", "-")
        record.input_fp = getattr(record, "input_fp", "-")
        return True


@pytest.fixture(scope="session", autouse=True)
def _banner():
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(_RunFilter())
    import fastapi
    lines = [
        "=" * 72,
        f"TEST SESSION START python={sys.version.split()[0]} platform={platform.platform()}",
        f"dependencies: pyarrow={pa.__version__} fastapi={fastapi.__version__} "
        f"pytest={pytest.__version__}",
        "=" * 72,
    ]
    for line in lines:
        print(line, file=sys.stderr)
    yield


@pytest.fixture()
def settings(tmp_path):
    from app.config import Settings
    return Settings(
        app_name="arrow-test",
        host="127.0.0.1",
        port=0,
        database_path=str(tmp_path / "test_meta.db"),
        log_level="INFO",
        log_format="dev",
        preview_limit=8,
    )


@pytest.fixture()
def service(settings):
    from app.service.service import ColumnService
    from app.store.metadata import MetadataStore
    store = MetadataStore(settings.resolved_db_path())
    svc = ColumnService(store)
    yield svc
    store.close()


@pytest.fixture()
def client(settings):
    from fastapi.testclient import TestClient
    from app.api.app import create_app
    app = create_app(settings)
    with TestClient(app) as c:
        yield c
    app.state.store.close()
