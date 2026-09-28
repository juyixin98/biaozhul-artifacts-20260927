"""Conftest for the independent suite: load the externally generated vectors.

The vectors are produced by ``reference_impl.py`` (a from-scratch
implementation that never imports ``smt``). They are checked in to git so
the cross-validation runs without a generation step; regenerate with:

    python independent_tests/generate_vectors.py
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (os.path.join(ROOT, "src"), HERE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from reference_impl import check as reference_check  # noqa: E402


@pytest.fixture(scope="session")
def vectors():
    path = os.path.join(HERE, "vectors.json")
    assert os.path.exists(path), "run independent_tests/generate_vectors.py first"
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def ref_check():
    return reference_check
