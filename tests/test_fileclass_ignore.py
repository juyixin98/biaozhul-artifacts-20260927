"""Text/binary classification and ignore-policy semantics."""

from __future__ import annotations

import pytest

from secretscan.fileclass import BINARY, TEXT, classify_bytes
from secretscan.ignore import IgnorePolicy


@pytest.mark.parametrize(
    "data,expected",
    [
        (b"hello world\n", TEXT),
        ("héllo".encode("utf-8"), TEXT),
        (b"abc\x00def", BINARY),
        (b"\xff\xfe\x00\x01binary", BINARY),
        (b"\x80\x81\x82 not utf8", BINARY),
    ],
)
def test_classify(data, expected):
    assert classify_bytes(data) == expected


def test_directory_pattern_prunes_at_any_depth():
    pol = IgnorePolicy((".git/", "vendor/"))
    assert pol.is_dir_ignored(".git")
    assert pol.is_dir_ignored("vendor")
    assert pol.is_dir_ignored("a/vendor")
    assert pol.is_file_ignored("vendor/lib/dep.txt")
    assert pol.is_file_ignored("a/.git/config")
    assert not pol.is_file_ignored("src/app.py")


def test_basename_glob_matches_files_at_any_depth():
    pol = IgnorePolicy(("*.example.env",))
    assert pol.is_file_ignored("config/app.example.env")
    assert pol.is_file_ignored("deep/nested/x.example.env")
    assert not pol.is_file_ignored("config/app.env")


def test_anchored_pattern_does_not_match_same_name_elsewhere():
    pol = IgnorePolicy(("config/app.env",))
    assert pol.is_file_ignored("config/app.env")
    assert not pol.is_file_ignored("other/config/app.env")


def test_double_star_crosses_directories():
    # Implemented as a/(?:.*/)?b — a superset of gitignore semantics that also
    # accepts zero crossed segments; documented in secretscan/ignore.py.
    pol = IgnorePolicy(("a/**/b",))
    assert pol.is_file_ignored("a/x/b")
    assert pol.is_file_ignored("a/x/y/z/b")
    assert pol.is_file_ignored("a/b")
    assert not pol.is_file_ignored("other/a/x/b")
