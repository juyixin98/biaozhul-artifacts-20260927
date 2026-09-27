"""Shared pytest helpers: load independently generated fixtures."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "generated"


def pytest_configure(config: object) -> None:
    """Ensure on-disk fixtures exist; regenerate from the independent builder."""
    marker = FIX / "baseline.ts"
    if not marker.exists():
        subprocess.run(
            [sys.executable, "-m", "scripts.make_fixtures"],
            cwd=ROOT, check=True, capture_output=True)


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    if not (FIX / "baseline.ts").exists():
        subprocess.run(
            [sys.executable, "-m", "scripts.make_fixtures"],
            cwd=ROOT, check=True, capture_output=True)
    return FIX


def load_fixture(fixture_dir: Path, name: str) -> tuple[bytes, dict]:
    data = (fixture_dir / f"{name}.ts").read_bytes()
    manifest = json.loads(
        (fixture_dir / f"{name}.expected.json").read_text())
    return data, manifest
