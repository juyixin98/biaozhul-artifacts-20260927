import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE_DIR = ROOT / "fixtures" / "data"

from app.config import Settings  # noqa: E402
from app.media.parser import load_segment  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=str(tmp_path / "jobs.db"),
        log_dir=str(tmp_path / "logs"),
        fixture_dir=str(FIXTURE_DIR),
    )


def fixture_path(name: str) -> Path:
    return FIXTURE_DIR / name


@pytest.fixture
def load():
    def _load(name):
        return load_segment(fixture_path(name))
    return _load
