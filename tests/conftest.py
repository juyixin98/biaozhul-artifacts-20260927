"""Pytest configuration: import path, logging and shared service fixtures."""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

# Stream audit records to a per-run log file so correlation ids (run id),
# service version, progress and decision basis are reviewable after the run.
LOG_DIR = ROOT / "tests" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
_handler = logging.FileHandler(LOG_DIR / "test-run.log", mode="w", encoding="utf-8")
_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
)
_root = logging.getLogger("archguard")
_root.setLevel(logging.DEBUG)
_root.addHandler(_handler)

from archguard.audit import AuditLogger  # noqa: E402
from archguard.config import Config  # noqa: E402
from archguard.engine import Engine  # noqa: E402
from archguard.isolation import ensure_home  # noqa: E402
from archguard.store import Store  # noqa: E402


@pytest.fixture
def svc(tmp_path):
    """A fresh Engine rooted at an isolated temporary service home."""
    home = tmp_path / "home"
    cfg = Config(**{**Config.load().to_dict(), "home": home.resolve()})
    ensure_home(cfg.home)
    store = Store(cfg.home)
    audit = AuditLogger(cfg.home)
    audit.set_sink(store.record_event)
    engine = Engine(cfg, store, audit)
    try:
        yield {
            "engine": engine,
            "store": store,
            "audit": audit,
            "config": cfg,
            "home": cfg.home,
        }
    finally:
        store.close()


@pytest.fixture
def canary(tmp_path):
    """Sentinel tree OUTSIDE the service home that rejection must not touch."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_bytes(b"untouched")
    keep = outside / "keep"
    keep.mkdir()
    (keep / "n.txt").write_bytes(b"also-untouched")

    def snapshot() -> dict[str, str]:
        result = {}
        for p in sorted(outside.rglob("*")):
            rel = str(p.relative_to(outside))
            if p.is_symlink():
                result[rel] = "symlink->" + str(p.readlink())
            elif p.is_file():
                result[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            else:
                result[rel] = "dir"
        return result

    return {"path": outside, "snapshot": snapshot}
