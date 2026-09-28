"""Shared pytest fixtures and path setup."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from basefee_model.fixtures import wallet
from basefee_model.storage.store import IndexStore
from basefee_model.replay.events import EventLog


VECTORS_PATH = os.path.join(os.path.dirname(__file__), "vectors",
                            "hand_vectors.json")
ORACLE_PATH = os.path.join(os.path.dirname(__file__), "oracle")
sys.path.insert(0, ORACLE_PATH)


@pytest.fixture
def funder():
    return wallet("funder")


@pytest.fixture
def receiver():
    return wallet("receiver")


@pytest.fixture
def funded_alloc(funder):
    # Generous but realistic balance (well under int64 money concerns at the
    # gas caps the tests use).
    return {funder.address_hex: 10 ** 24}


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test.db"
    s = IndexStore(str(db))
    yield s
    s.close()


@pytest.fixture
def silent_log():
    return EventLog(enabled=False)


@pytest.fixture
def hand_vectors():
    import json
    with open(VECTORS_PATH, encoding="utf-8") as fh:
        return json.load(fh)
