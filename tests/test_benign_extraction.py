"""Benign extraction tests: correctness of the controlled output.

These verify bytes on disk, directory creation, in-root symlink behavior, and
that every produced path is confined to the isolated run output directory.
"""
from __future__ import annotations

import os
from pathlib import Path

from tests.fixtures import builder as fb


def _all_paths_under(root: Path) -> None:
    root = root.resolve()
    for p in root.rglob("*"):
        rp = p.resolve() if not p.is_symlink() else p
        # Symlink itself must live under root even if its target is lexical.
        assert str(p).startswith(str(root)), p


def test_benign_zip_extracts_bytes(service, settings):
    data = fb.zip_bytes([
        {"name": "dir/", "is_dir": True},
        {"name": "dir/hello.txt", "data": b"hello world"},
        {"name": "top.txt", "data": b"plain"},
    ])
    result = service.extract(data, "good.zip")
    assert result.verdict == "extracted", result.error
    out = result.workspace.output_dir
    assert (out / "dir" / "hello.txt").read_bytes() == b"hello world"
    assert (out / "top.txt").read_bytes() == b"plain"
    assert (out / "dir").is_dir()
    assert result.manifest["total_bytes"] == len(b"hello world") + len(b"plain")
    _all_paths_under(out)


def test_benign_tar_extracts_bytes(service, settings):
    data = fb.tar_bytes([
        {"name": "d", "is_dir": True},
        {"name": "d/a.txt", "data": b"tar-content"},
    ])
    result = service.extract(data, "good.tar")
    assert result.verdict == "extracted", result.error
    assert (result.workspace.output_dir / "d" / "a.txt").read_bytes() == b"tar-content"


def test_implicit_parent_directories_created(service, settings):
    # No explicit directory entries; kernel must create the parents.
    data = fb.zip_bytes([{"name": "a/b/c/deep.txt", "data": b"deep"}])
    result = service.extract(data, "implicit.zip")
    assert result.verdict == "extracted", result.error
    p = result.workspace.output_dir / "a" / "b" / "c" / "deep.txt"
    assert p.read_bytes() == b"deep"


def test_in_root_symlink_is_created_and_resolves(service, settings):
    data = fb.zip_bytes([
        {"name": "real/f.txt", "data": b"target-bytes"},
        {"name": "alias", "link": "real/f.txt"},
    ])
    result = service.extract(data, "links.zip")
    assert result.verdict == "extracted", result.error
    out = result.workspace.output_dir
    link = out / "alias"
    assert link.is_symlink()
    assert os.readlink(link) == "real/f.txt"
    # Reading through the in-root symlink yields the in-root target's bytes.
    assert link.read_bytes() == b"target-bytes"
    _all_paths_under(out)


def test_symlink_directory_prefix_redirects_within_root(service, settings):
    # "a/dir" is a symlink to "../store" (i.e. root/store); a file declared as
    # a/dir/one.txt must physically land at the resolved in-root location
    # store/one.txt, never outside the root.
    data = fb.zip_bytes([
        {"name": "store/existing.txt", "data": b"existing"},
        {"name": "a/dir", "link": "../store"},
        {"name": "a/dir/one.txt", "data": b"via-prefix"},
    ])
    result = service.extract(data, "prefix.zip")
    assert result.verdict == "extracted", (
        result.error.category if result.error else result.verdict
    )
    out = result.workspace.output_dir
    # Symlink itself and the resolved physical target both exist.
    assert (out / "a" / "dir").is_symlink()
    assert os.readlink(out / "a" / "dir") == "../store"
    assert (out / "store" / "one.txt").read_bytes() == b"via-prefix"
    # Reaching the bytes through the symlinked prefix works and stays in-root.
    assert (out / "a" / "dir" / "one.txt").read_bytes() == b"via-prefix"
    _all_paths_under(out)


def test_symlink_alias_collision_is_rejected(service, settings):
    # Two distinct canonical names converging on ONE physical path (one through
    # a symlink prefix) must be rejected as an alias conflict.
    data = fb.zip_bytes([
        {"name": "store/two.txt", "data": b"two"},
        {"name": "a/dir", "link": "../store"},
        {"name": "a/dir/two.txt", "data": b"two"},
    ])
    result = service.extract(data, "alias.zip")
    assert result.verdict == "rejected"
    assert result.error.category == "symlink_alias"


def test_inspect_does_not_extract(service, settings):
    data = fb.zip_bytes([{"name": "a.txt", "data": b"x"}])
    result = service.inspect(data, "g.zip")
    assert result.verdict == "accepted"
    assert result.plan.stats.files == 1
    # Inspect must never create an output directory.
    assert not result.workspace.output_dir.exists()


def test_repeated_runs_are_isolated_state(service, settings):
    data = fb.zip_bytes([{"name": "a.txt", "data": b"x"}])
    r1 = service.extract(data, "a.zip")
    r2 = service.extract(data, "a.zip")
    assert r1.run_id != r2.run_id
    assert r1.workspace.root != r2.workspace.root
    assert r1.workspace.root.exists() and r2.workspace.root.exists()
