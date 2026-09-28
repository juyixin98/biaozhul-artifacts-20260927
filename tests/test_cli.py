"""CLI end-to-end tests via subprocess (also proves packaging entrypoints)."""

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "secretscan.cli", *args],
        cwd=REPO_ROOT, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_json_report_against_fixture_repo(tmp_path):
    db = tmp_path / "cli.db"
    code, out, err = _run_cli(
        "scan", str(REPO_ROOT / "fixtures" / "repo"),
        "--workspace", str(db),
        "--baseline", str(REPO_ROOT / "fixtures" / "baseline.toml"),
        "--request-id", "cli-r1", "--actor-id", "cli-actor",
        "--json")
    assert code == 0, err
    report = json.loads(out)
    assert report["request_id"] == "cli-r1"
    assert report["actor_id"] == "cli-actor"
    counts = report["counts"]
    assert counts["findings_new"] == 6
    assert counts["findings_baseline_exempt"] == 1
    assert counts["files_unscanned"] == 1  # the symlink
    # No raw secret in stdout.
    assert "ghp_1eAoPJ4BzuZNn3XmX7lgARsGjSQZTBCSEIka" not in out
    # Versions are explained.
    assert report["versions"]["rule_pack"].startswith("rules:2026.09.0")
    assert report["versions"]["scope_pack"].startswith("scope:2026.09.0")


def test_cli_text_report_lists_unscanned_separately(tmp_path):
    db = tmp_path / "cli2.db"
    code, out, err = _run_cli(
        "scan", str(REPO_ROOT / "fixtures" / "repo"),
        "--workspace", str(db))
    assert code == 0, err
    assert "NOT SCANNED" in out
    assert "link-to-aws.ini" in out
    assert "symlink" in out
    assert "KNOWN-FIXED" in out or "known-fixed" in out


def test_cli_bad_config_exits_2(tmp_path):
    code, out, err = _run_cli(
        "scan", str(REPO_ROOT / "fixtures" / "repo"),
        "--rules", str(tmp_path / "missing.toml"),
        "--workspace", str(tmp_path / "x.db"))
    assert code == 2
    assert "configuration error" in err


def test_cli_two_runs_show_lifecycle_and_no_raw_secret_in_log(tmp_path):
    db = tmp_path / "cli3.db"
    log_file = tmp_path / "audit.log"
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = "ghp_1eAoPJ4BzuZNn3XmX7lgARsGjSQZTBCSEIka"
    (repo / "a.py").write_text(f'TOKEN = "{secret}"\n')
    code1, _, err1 = _run_cli(
        "scan", str(repo), "--workspace", str(db),
        "--log-file", str(log_file))
    assert code1 == 0, err1
    # Remove the secret (same file, changed content) -> known-fixed.
    (repo / "a.py").write_text("TOKEN = rotated-clean\n")
    code2, out2, _ = _run_cli(
        "scan", str(repo), "--workspace", str(db),
        "--log-file", str(log_file), "--json")
    assert code2 == 0
    report = json.loads(out2)
    assert report["counts"]["findings_known_fixed"] == 1
    assert secret not in out2
    log_text = log_file.read_text()
    assert secret not in log_text
    for line in log_text.splitlines():
        json.loads(line)  # every log line is valid JSON
