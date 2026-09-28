"""CLI smoke tests exercising the command-line entry point end to end."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from . import fixtures as fx

ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, name: str, obj) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def _run(args, env_data_dir):
    env_extra = {"OSDIFF_DATA_DIR": str(env_data_dir)}
    import os
    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, "-m", "osdiff.cli", *args],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )


def test_cli_diff_exit_codes_and_json(tmp_path):
    old = _write(tmp_path, "old.json", fx.EMPTY)
    new = _write(tmp_path, "new.json", fx.PHOTO_V2_EXPAND)
    r = _run(["diff", old, new, "--json"], tmp_path / "data")
    # exit code 2 signals proven expansion
    assert r.returncode == 2, r.stderr
    body = json.loads(r.stdout)
    assert body["expands"] is True
    assert body["counts"]["EXPANSION_PROVEN"] >= 1


def test_cli_unknown_condition_is_flagged_possible(tmp_path):
    old = _write(tmp_path, "old.json", fx.NEG_V1)
    new = _write(tmp_path, "new.json", fx.NEG_V2)
    r = _run(["diff", old, new, "--json"], tmp_path / "data")
    body = json.loads(r.stdout)
    assert body["counts"]["EXPANSION_POSSIBLE"] >= 1
    assert body["possibly_expands"] is True


def test_cli_parse_failure_is_nonzero_with_class(tmp_path):
    bad = _write(tmp_path, "bad.json", fx.BAD_GLOB)
    ok = _write(tmp_path, "ok.json", fx.EMPTY)
    r = _run(["diff", bad, ok], tmp_path / "data")
    assert r.returncode == 1
    err = json.loads(r.stderr)
    assert err["failure_code"] == "PARSE_ERROR"
    assert err["details"]["errors"]


def test_cli_verify_and_audit_chain(tmp_path):
    policy = _write(tmp_path, "p.json", fx.NEG_V2)
    request = _write(tmp_path, "req.json", {
        "principal": "alice", "action": "s3:GetObject", "resource": "docs/x",
        "attributes": {"department": {"__unknown__": True}},
    })
    r = _run(["verify", policy, request], tmp_path / "data")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["verdict"] == "UNKNOWN"

    chain = _run(["audit-verify"], tmp_path / "data")
    assert chain.returncode == 0
    assert json.loads(chain.stdout)["ok"] is True
