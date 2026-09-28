"""Happy-path extraction: exact on-disk result, symlinks and verification."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fixtures_archive import ZipSpec, build_tar, build_zip


def test_zip_benign_extraction(svc):
    data = build_zip([
        ZipSpec("a/hello.txt", data=b"hello"),
        ZipSpec("a/world.txt", data=b"world"),
        ZipSpec("empty/", kind="dir"),
        ZipSpec("a/nested/deep.txt", data=b"deep"),
    ])
    v = svc["engine"].inspect(data, input_name="ok.zip")
    assert v.status == "accepted"
    assert v.accepted is True
    assert v.category is None

    run_dir = svc["home"] / "runs" / v.run_id
    out = run_dir / "out"
    assert (out / "a" / "hello.txt").read_bytes() == b"hello"
    assert (out / "a" / "world.txt").read_bytes() == b"world"
    assert (out / "a" / "nested" / "deep.txt").is_file()
    assert (out / "empty").is_dir()

    # File list reports exact sizes and independently-computed sha256 values.
    by_path = {f["declared_path"]: f for f in v.files}
    assert by_path["a/hello.txt"]["size"] == 5
    assert by_path["a/hello.txt"]["sha256"] == hashlib.sha256(b"hello").hexdigest()

    # Independent verification counts agree with what's on disk.
    assert v.verify["files"] == 3
    # Staged upload bytes are removed once the run is finalized.
    assert list((svc["home"] / "spool").iterdir()) == []
    assert v.usage["file_count"] == 3
    assert v.usage["total_bytes"] == len(b"hello") + len(b"world") + len(b"deep")


def test_tar_benign_extraction(svc):
    data = build_tar([
        ("dir", "logs"),
        ("file", "logs/a.log", b"log-line\n"),
        ("file", "top.txt", b"top"),
    ])
    v = svc["engine"].inspect(data, input_name="ok.tar")
    assert v.status == "accepted", v.detail
    out = svc["home"] / "runs" / v.run_id / "out"
    assert (out / "logs" / "a.log").read_bytes() == b"log-line\n"
    assert (out / "top.txt").read_bytes() == b"top"


def test_relative_symlink_extracts(svc):
    data = build_zip([
        ZipSpec("link", kind="symlink", target=b"target.txt"),
        ZipSpec("target.txt", data=b"payload"),
    ])
    v = svc["engine"].inspect(data, input_name="link.zip")
    assert v.status == "accepted", v.detail
    out = svc["home"] / "runs" / v.run_id / "out"
    link = out / "link"
    assert link.is_symlink()
    assert os.readlink(link) == "target.txt"
    # Link resolves inside the root to the real file.
    assert link.resolve() == (out / "target.txt").resolve()
    assert link.read_bytes() == b"payload"


def test_directory_symlink_redirects_physical_path(svc):
    # "d" is a link to "realdir"; "d/g.txt" must physically land in realdir/.
    data = build_zip([
        ZipSpec("d", kind="symlink", target=b"realdir"),
        ZipSpec("realdir/f.txt", data=b"F"),
        ZipSpec("d/g.txt", data=b"GG"),
    ])
    v = svc["engine"].inspect(data, input_name="dirlink.zip")
    assert v.status == "accepted", v.detail
    out = svc["home"] / "runs" / v.run_id / "out"
    assert (out / "realdir" / "f.txt").read_bytes() == b"F"
    assert (out / "realdir" / "g.txt").read_bytes() == b"GG"
    # The same content is reachable through the link alias.
    assert (out / "d" / "g.txt").read_bytes() == b"GG"


def test_dot_and_slash_normalized(svc):
    data = build_zip([
        ZipSpec("./a/./b.txt", data=b"x"),
        ZipSpec("a//c.txt", data=b"y"),
    ])
    v = svc["engine"].inspect(data, input_name="dots.zip")
    assert v.status == "accepted", v.detail
    out = svc["home"] / "runs" / v.run_id / "out"
    assert (out / "a" / "b.txt").is_file()
    assert (out / "a" / "c.txt").is_file()


def test_outputs_contained_in_home(svc, tmp_path):
    data = build_zip([ZipSpec("a.txt", data=b"x")])
    v = svc["engine"].inspect(data, input_name="ok.zip")
    assert v.status == "accepted"
    home = Path(os.path.realpath(svc["home"]))
    out = Path(os.path.realpath(svc["home"] / "runs" / v.run_id / "out"))
    # The materialized output root, and every node under it, resolves to a
    # location inside the service home.
    assert str(home) == os.path.commonpath([str(home), str(out)])
    for p in out.rglob("*"):
        resolved = Path(os.path.realpath(p))
        assert str(home) == os.path.commonpath([str(home), str(resolved)])
