"""Local synthetic fixtures shared across tests.

No production accounts, no real data: every secret and collection is generated
here on the local machine. Secrets are deliberately small/printable so tests
can assert exact literal values.
"""
from __future__ import annotations

import itertools
import os
import sys

# Make ``import app`` and ``from oracle import ...`` work regardless of pytest
# rootdir configuration.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import pytest  # noqa: E402

from app.audit import Auditor  # noqa: E402
from app.config import Settings  # noqa: E402
from app.core.kernel import Kernel  # noqa: E402
from app.state import Store  # noqa: E402

# A small fixed secret (well within one 31-byte block).
SECRET_A = b"the quick brown fox"
# A multi-block secret (67 bytes -> ceil(67/31) = 3 blocks) to exercise
# independent per-block polynomials.
SECRET_LONG = b"x" * 31 + b"y" * 31 + b"z" * 5
SECRET_EMPTY = b""


@pytest.fixture()
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture()
def store(tmp_db):
    s = Store(tmp_db)
    yield s
    s.close()


@pytest.fixture()
def auditor(store):
    return Auditor(store, to_stderr=False)


@pytest.fixture()
def kernel(store, auditor):
    return Kernel(store, auditor)


def make_collection(kernel: Kernel, secret: bytes, t: int, n: int,
                    request_id: str = "req_setup", cid: str = "coll_test"):
    out = kernel.create_collection(
        request_id=request_id,
        secret=secret,
        threshold=t,
        total=n,
        collection_id=cid,
    )
    return out


def all_subsets(xs, size):
    return list(itertools.combinations(xs, size))
