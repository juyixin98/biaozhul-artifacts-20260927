"""Pytest configuration: make package importable and provide the deterministic
synthetic fixture manifest + a fresh service factory."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from localffg.config import AppConfig  # noqa: E402
from localffg.epochs import ValidatorRegistry  # noqa: E402
from localffg.fixtures_builder import build_fixture_set  # noqa: E402
from localffg.logging_utils import JsonRunLogger  # noqa: E402
from localffg.service import VoteService  # noqa: E402


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("fixtures")
    build_fixture_set(d)
    return d


@pytest.fixture(scope="session")
def manifest(fixture_dir) -> dict:
    return json.loads((fixture_dir / "fixture_manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def domain(manifest) -> bytes:
    return manifest["domain"].encode("ascii")


@pytest.fixture()
def registry(manifest) -> ValidatorRegistry:
    return ValidatorRegistry.from_json(manifest["registry"])


@pytest.fixture()
def oracle_registry(manifest):
    sys.path.insert(0, str(Path(__file__).parent))
    from independent_oracle import OracleRegistry

    return OracleRegistry.from_manifest(manifest["registry"])


@pytest.fixture()
def make_service(tmp_path):
    created: list[VoteService] = []

    def _make(manifest_registry: dict | None = None, *, echo: bool = False):
        cfg = AppConfig(db_path=str(tmp_path / f"svc-{len(created)}.db"))
        logger = JsonRunLogger(run_id=f"test-{len(created)}", echo=echo)
        svc = VoteService(cfg, logger=logger)
        if manifest_registry is not None:
            svc.install_registry(ValidatorRegistry.from_json(manifest_registry))
        created.append(svc)
        return svc

    yield _make
    for svc in created:
        svc.close()
