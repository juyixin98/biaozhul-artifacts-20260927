"""Shared test fixtures and — importantly — INDEPENDENT reference values.

The constants below are plain literals / independent stdlib re-implementations.
They do not call the production code: tests assert the scanner agrees with
these independent answers, rather than regenerating expected output with the
code under test.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import logging
import math
from pathlib import Path

import pytest

from secretscan import audit, service, state
from secretscan.baseline import load_baseline
from secretscan.config import load_rule_pack, load_scope_pack
from secretscan.security import Fingerprinter

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_REPO = REPO_ROOT / "fixtures" / "repo"
FIXTURE_BASELINE = REPO_ROOT / "fixtures" / "baseline.toml"
RULES_PACK = REPO_ROOT / "config" / "rules" / "default.toml"
SCOPE_PACK = REPO_ROOT / "config" / "scopes" / "default.toml"
TINY_SCOPE_PACK = REPO_ROOT / "tests" / "config" / "scope-tiny.toml"

# Development pepper used by every fixture (also baked into the baseline).
DEV_PEPPER = "dev-pepper-do-not-use-in-production-opp275"
DEV_PEPPER_ID = hashlib.sha256(DEV_PEPPER.encode()).hexdigest()[:12]

# --- Synthetic secret literals (FAKE; never validated over any network) ----
GHP_TOKEN = "ghp_1eAoPJ4BzuZNn3XmX7lgARsGjSQZTBCSEIka"
SLACK_TOKEN = ("xoxb-123456789012-1234567890123-"
               "AbCdEfGhIjKlMnOpQrStUvWx")
AWS_ID = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
GENERIC_TOKEN = "9fKx2pL7vQmR4nT8wY3cB6hJ1dS0gZ5aX8eW2u"
LATIN1_TOKEN = "7qQwErTyUiOpAsDfGhJkLzXcVbNm1234567890AB"
HIGH_ENTROPY_PROSE = "Xk9pL2vQ7mR4nT8wY3cB6hJ1fD0sG5aZ"
BLOB_HASH = "2ObHD4nCs7Z5IQmUBLf0ltrGN16KhE8cFkxYweJy"
LOW_ENTROPY_PASSWORD = "hunter2"

# --- Independent expected masks (mirroring the security kernel rule) -------
def expected_mask(value: str) -> str:
    n = len(value)
    if n <= 12:
        return "*" * n
    keep = min(4, (n - 4) // 2)
    return value[:keep] + "*" * (n - 2 * keep) + value[-keep:]


EXPECTED_MASKS = {
    "ghp": expected_mask(GHP_TOKEN),
    "slack": expected_mask(SLACK_TOKEN),
    "aws_id": expected_mask(AWS_ID),
    "aws_secret": expected_mask(AWS_SECRET),
    "generic": expected_mask(GENERIC_TOKEN),
}

# --- Independent expected fingerprints (stdlib hmac, no production import) --
def expected_fingerprint(value: str, pepper: str = DEV_PEPPER) -> str:
    return hmac.new(pepper.encode(), value.encode(), hashlib.sha256).hexdigest()


# --- Independent entropy (own implementation, not secretscan.entropy) -------
def expected_entropy(text: str) -> float:
    counts = collections.Counter(text)
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


@pytest.fixture(scope="session")
def rule_pack():
    return load_rule_pack(RULES_PACK)


@pytest.fixture(scope="session")
def scope_pack():
    return load_scope_pack(SCOPE_PACK)


@pytest.fixture(scope="session")
def tiny_scope_pack():
    return load_scope_pack(TINY_SCOPE_PACK)


@pytest.fixture
def fingerprinter():
    return Fingerprinter(DEV_PEPPER)


@pytest.fixture
def baseline(fingerprinter):
    return load_baseline(FIXTURE_BASELINE, fingerprinter)


@pytest.fixture
def db_conn(tmp_path):
    conn = state.connect(tmp_path / "workspace.db")
    yield conn
    conn.close()


@pytest.fixture
def quiet_logger():
    logger, _ = audit.configure_logging(None, level=logging.WARNING)
    return logger


@pytest.fixture
def service_factory(db_conn, rule_pack, scope_pack, fingerprinter,
                    quiet_logger):
    """Factory building a ScanService, optionally with a baseline."""
    def _make(baseline_obj=None, scope=None):
        return service.ScanService(
            db_conn, rule_pack, scope or scope_pack, fingerprinter,
            quiet_logger, baseline_obj)
    return _make


@pytest.fixture
def snapshot_factory(tmp_path):
    """Create throwaway repository snapshots with explicit file contents."""
    def _make(files: dict[str, bytes | str]) -> Path:
        root = tmp_path / "snapshot"
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content, encoding="utf-8")
        return root
    return _make
