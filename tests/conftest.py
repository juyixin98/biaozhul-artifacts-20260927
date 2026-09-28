"""Shared test fixtures."""
from __future__ import annotations

import pytest

from app.diagnostics import RequestDiagnostics
from app.storage.registry import VersionRegistry
from app.storage.repository import DictionaryRepository


@pytest.fixture
def registry(tmp_path):
    """A registry on a throwaway DB (no seeding; tests publish their own data)."""
    repo = DictionaryRepository(tmp_path / "test_dict.db")
    return VersionRegistry(repo, unknown_char_cost=8.0, min_word_cost=0.01,
                           max_word_length=32)


@pytest.fixture
def diag():
    return RequestDiagnostics(request_id="tst-request-0001")
