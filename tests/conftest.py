import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import build_all  # noqa: E402

from colaudit import adapter as ad  # noqa: E402
from colaudit.catalog import Catalog  # noqa: E402
from colaudit.config import Settings  # noqa: E402


@pytest.fixture(scope="session")
def fixture_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("fx")
    build_all(root)
    return root


@pytest.fixture
def tmp_home(tmp_path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    fx = tmp_path / "fx_copy"
    home.mkdir()
    return home, fx


@pytest.fixture
def settings(tmp_path, fixture_root) -> Settings:
    return Settings(
        home=tmp_path / "home",
        fixtures_dir=fixture_root,
        mask_sensitive=True,
    ).ensure_dirs()


@pytest.fixture
def catalog(settings) -> Catalog:
    return Catalog(settings.effective_db_path)


@pytest.fixture
def load():
    def _load(root: Path, name: str) -> ad.Dataset:
        return ad.load_dataset(root / name)
    return _load
