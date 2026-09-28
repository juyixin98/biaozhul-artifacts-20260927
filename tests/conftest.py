"""Shared pytest fixtures.

Tests use their own temporary data dir (ZINDEX_* env override) so a stale
``data/`` tree from a demo run can never influence them.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ZINDEX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ZINDEX_CATALOG_PATH", str(tmp_path / "catalog.sqlite"))
    monkeypatch.setenv("ZINDEX_LOG_FILE", str(tmp_path / "test.log"))
    from zindex.config import Settings
    from zindex.catalog import Catalog

    settings = Settings.load()
    catalog = Catalog(settings.catalog_path)
    yield {"settings": settings, "catalog": catalog, "data_dir": data_dir, "tmp": tmp_path}
    catalog.close()
