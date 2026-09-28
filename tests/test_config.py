"""Versioned rule/scope pack loading, validation and ignore semantics."""

import pytest

from secretscan.config import (ConfigError, _glob_to_regex, load_rule_pack,
                               load_scope_pack, load_settings)
from conftest import RULES_PACK, SCOPE_PACK
import re


def test_rule_pack_exposes_version_and_fingerprint():
    pack = load_rule_pack(RULES_PACK)
    assert pack.version == "2026.09.0-rules.1"
    assert pack.fingerprint().startswith("rules:2026.09.0-rules.1@sha256:")
    # Fingerprint changes when bytes change.
    assert len(pack.source_sha256) == 64


def test_rule_pack_has_all_expected_rules_in_priority_order():
    pack = load_rule_pack(RULES_PACK)
    assert [r.id for r in pack.rules] == [
        "github-classic-pat", "aws-access-key-id", "aws-secret-access-key",
        "slack-bot-token", "private-key-pem", "generic-assigned-secret"]
    ghp = pack.rules[0]
    assert ghp.min_entropy == 3.5 and ghp.confidence == "high"


def test_scope_pack_limits_and_version():
    scope = load_scope_pack(SCOPE_PACK)
    assert scope.version == "2026.09.0-scope.1"
    assert scope.max_file_bytes == 1024 * 1024
    assert scope.binary_min_run == 8


@pytest.mark.parametrize("pattern,path,is_dir,expected", [
    (".git/", ".git", True, True),
    (".git/", ".git/config", False, True),
    (".git/", "src/.gitkeep", False, False),
    ("__pycache__/", "a/__pycache__", True, True),
    ("*.pyc", "a/b/c.pyc", False, True),
    ("*.pyc", "a/b/c.py", False, False),
    ("vendor/", "vendor/lib/x.py", False, True),
    ("vendor/", "notvendor/x.py", False, False),
    ("dist/", "packages/dist", True, True),
])
def test_glob_translation(pattern, path, is_dir, expected):
    rx = re.compile(_glob_to_regex(pattern))
    target = path + "/" if is_dir else path
    assert (rx.search(target) is not None) is expected


def test_scope_ignore_matches_fixture_paths(scope_pack):
    assert scope_pack.is_ignored(".git/config", False)
    assert scope_pack.is_ignored(".git", True)
    assert scope_pack.is_ignored("src/__pycache__/x.pyc", False)
    assert not scope_pack.is_ignored("src/app.py", False)
    assert not scope_pack.is_ignored("config/aws-credentials.ini", False)


def test_missing_pack_files_raise_config_error(tmp_path):
    with pytest.raises(ConfigError, match="rule pack not found"):
        load_rule_pack(tmp_path / "nope.toml")
    with pytest.raises(ConfigError, match="scope pack not found"):
        load_scope_pack(tmp_path / "nope.toml")


def test_invalid_regex_is_rejected(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(
        '[meta]\nname="x"\nversion="1"\n\n'
        '[[rules]]\nid="r"\ndescription="d"\npattern="("\n'
        'secret_group=0\nentropy_group=0\nmin_entropy=0\n'
        'confidence="low"\n')
    with pytest.raises(ConfigError, match="invalid regex"):
        load_rule_pack(bad)


def test_duplicate_rule_ids_rejected(tmp_path):
    bad = tmp_path / "dup.toml"
    bad.write_text(
        '[meta]\nname="x"\nversion="1"\n\n'
        '[[rules]]\nid="r"\ndescription="d"\npattern="a"\n'
        'secret_group=0\nentropy_group=0\nmin_entropy=0\nconfidence="low"\n\n'
        '[[rules]]\nid="r"\ndescription="d2"\npattern="b"\n'
        'secret_group=0\nentropy_group=0\nmin_entropy=0\nconfidence="low"\n')
    with pytest.raises(ConfigError, match="duplicate rule ids"):
        load_rule_pack(bad)


def test_settings_reject_empty_pepper(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRETSCAN_PEPPER", "   ")
    with pytest.raises(ConfigError, match="pepper"):
        load_settings(tmp_path / "x.db")


def test_settings_reject_nonexistent_allowed_root(tmp_path):
    with pytest.raises(ConfigError, match="allowed root"):
        load_settings(tmp_path / "x.db",
                      allowed_roots=(tmp_path / "missing",))
