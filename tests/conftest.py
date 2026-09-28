import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
SNAPSHOT = FIXTURES / "snapshot"
BUILDER = ROOT / "tests" / "build_fixtures.py"


def _ensure_fixture() -> None:
    """(Re)build the deterministic fixture, including mode-000 file."""
    subprocess.run([sys.executable, str(BUILDER)], check=True, capture_output=True)


@pytest.fixture(scope="session", autouse=True)
def fixture_snapshot():
    _ensure_fixture()
    yield SNAPSHOT


@pytest.fixture()
def snapshot_copy(tmp_path):
    """A mutable copy of the fixture for delete/move/chmod tests."""
    dst = tmp_path / "snap"
    shutil.copytree(
        SNAPSHOT,
        dst,
        symlinks=True,
        ignore=shutil.ignore_patterns("huge.log", "no-read.log"),
    )
    # Rebuild the oversize file in the copy cheaply: one byte over the
    # configured limit is enough for the too_large classification.
    (dst / "huge.log").write_bytes(b"\x00" * (1_048_576 + 1))
    # Recreate the unreadable file with mode 000.
    noread = dst / "no-read.log"
    noread.write_text("secrets that cannot be read\nAKIAFAKE000000000004\n")
    os.chmod(noread, 0o000)
    yield dst
    # Avoid teardown failures on the mode-000 file.
    os.chmod(dst / "no-read.log", 0o644)


@pytest.fixture()
def fixed_keys():
    """Deterministic candidate-HMAC key material used by scanner tests."""
    return bytes([0x11]) * 32, bytes([0x22]) * 16
