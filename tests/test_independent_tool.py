"""Cross-check that the INDEPENDENT stdlib tool agrees with production code.

tools/make_baseline.py imports nothing from secretscan. If both fingerprint
implementations derive from HMAC-SHA256 they must agree; if someone changes
one without the other, this fails.
"""

import subprocess
import sys
from pathlib import Path

from conftest import DEV_PEPPER, DEV_PEPPER_ID, GHP_TOKEN, GENERIC_TOKEN
from secretscan.security import Fingerprinter

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "tools" / "make_baseline.py"


def _tool(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=REPO_ROOT, capture_output=True, text=True,
        env={**__import__("os").environ})
    return proc.returncode, proc.stdout, proc.stderr


def test_tool_uses_no_production_imports():
    source = TOOL.read_text()
    assert "import secretscan" not in source
    assert "from secretscan" not in source


def test_pepper_id_agrees():
    code, out, err = _tool("--pepper-id")
    assert code == 0, err
    assert out.strip() == DEV_PEPPER_ID


def test_fingerprints_agree_for_multiple_values():
    fp = Fingerprinter(DEV_PEPPER)
    for value in (GHP_TOKEN, GENERIC_TOKEN):
        code, out, err = _tool("--value", value,
                               "--rule-id", "generic-assigned-secret")
        assert code == 0, err
        tool_fp = next(
            line.split('"')[1] for line in out.splitlines()
            if line.startswith("fingerprint"))
        tool_mask = next(
            line.split('"')[1] for line in out.splitlines()
            if line.startswith("mask"))
        assert tool_fp == fp.fingerprint(value)
        assert "*" in tool_mask and value not in out


def test_baseline_fixture_fingerprint_reproduces():
    """The checked-in baseline's fingerprint must be reproducible by both."""
    import tomllib
    baseline = REPO_ROOT / "fixtures" / "baseline.toml"
    data = tomllib.loads(baseline.read_text())
    stored = data["exemptions"][0]["fingerprint"]
    fp = Fingerprinter(DEV_PEPPER)
    assert stored == fp.fingerprint(GENERIC_TOKEN)
