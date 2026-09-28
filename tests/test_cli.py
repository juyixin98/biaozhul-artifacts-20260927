"""CLI integration tests (subprocess-free, invoke main directly)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sqlguard import cli
from sqlguard.state.fixture import create_fixture
from tests.conftest import FIXTURE_SCHEMA, FIXTURE_SEED


@pytest.fixture()
def isolated_env(tmp_path: Path) -> Path:
    repo_root = Path(__file__).resolve().parent.parent
    fix = tmp_path / "fixture"
    fix.mkdir()
    create_fixture(fix / "fixture.db", FIXTURE_SCHEMA, FIXTURE_SEED)
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(
        f"policy_path: {repo_root / 'config' / 'policy.yaml'}\n"
        f"fixture_dir: {fix}\n"
        f"audit_db_path: {tmp_path / 'audit.db'}\n"
        f"audit_key_path: {tmp_path / 'audit.key'}\n"
        f'log_level: WARNING\n')
    return cfg


def test_cli_accept_returns_zero(isolated_env, capsys):
    rc = cli.main([
        "--config", str(isolated_env), "review",
        "--template", "SELECT id FROM users WHERE id = ?",
        "--params", '{"0": 1}'])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["verdict"] == "accept"
    assert out["request_id"].startswith("req_")


def test_cli_reject_returns_one(isolated_env, capsys):
    rc = cli.main([
        "--config", str(isolated_env), "review",
        "--template", "SELECT id FROM ?", "--params", '{"0":"users"}'])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["verdict"] == "reject"
    assert "VALUE_PARAM_AS_IDENTIFIER" in out["codes"]["reject"]


def test_cli_unanalyzable_returns_one(isolated_env, capsys):
    rc = cli.main(["--config", str(isolated_env), "review",
                   "--template", "SELECT '"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["verdict"] == "unanalyzable"


def test_cli_carries_request_id_and_redacts(isolated_env, capsys):
    secret = "cli-secret-value"
    rc = cli.main([
        "--config", str(isolated_env), "review",
        "--template", "SELECT id FROM users WHERE name = ?",
        "--params", json.dumps({"0": secret}),
        "--request-id", "cli-trace-1"])
    assert rc == 0
    printed = capsys.readouterr().out
    assert secret not in printed
    assert "cli-trace-1" in printed


def test_cli_verify_chain(isolated_env, capsys):
    cli.main(["--config", str(isolated_env), "review",
              "--template", "SELECT 1"])
    capsys.readouterr()  # discard review output
    rc = cli.main(["--config", str(isolated_env), "verify-chain"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
