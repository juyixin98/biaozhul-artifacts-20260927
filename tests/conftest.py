"""pytest 公共夹具。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.coding.params import TreeParams  # noqa: E402
from app.core.smt import SparseMerkleTree  # noqa: E402
from app.core.store import InMemoryStore  # noqa: E402
from tests.reference.naive_smt import NaiveSMT  # noqa: E402


@pytest.fixture
def params8() -> TreeParams:
    return TreeParams(key_len=1, depth=8)


@pytest.fixture
def tree8(params8):
    return SparseMerkleTree(InMemoryStore(), params8)


@pytest.fixture
def naive8() -> NaiveSMT:
    return NaiveSMT(key_len=1, depth=8)


@pytest.fixture
def params256() -> TreeParams:
    return TreeParams(key_len=32, depth=256)


@pytest.fixture
def golden_vectors() -> dict:
    path = ROOT / "samples" / "golden_vectors.json"
    return json.loads(path.read_text(encoding="utf-8"))
