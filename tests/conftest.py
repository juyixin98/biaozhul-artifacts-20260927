"""Shared pytest fixtures."""
from __future__ import annotations

import os
import sys

import pytest

# Make src/ importable without an install step (pytest.ini also sets pythonpath,
# this is a belt-and-braces path for direct editor runs).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
IND = os.path.join(ROOT, "independent_tests")
if IND not in sys.path:
    sys.path.insert(0, IND)


@pytest.fixture()
def keys():
    from smt.crypto import normalize_key

    return {
        "ka": normalize_key("00" * 30 + "ab" + "00"),  # shares 248-bit prefix with kb
        "kb": normalize_key("00" * 30 + "ab" + "01"),
        "kc": bytes.fromhex("ff" * 32),
        "abs_sub": bytes.fromhex("00" * 30 + "ab" + "02"),
        "abs_far": bytes.fromhex("11" * 32),
    }


@pytest.fixture()
def mem_tree():
    from smt.kernel import MemoryNodeStore, SparseMerkleTree

    store = MemoryNodeStore()
    return store, SparseMerkleTree(store)
