"""Rejection fixtures: exact failure category + zero change outside isolation.

For every hostile archive we assert three things:

1. the verdict status is ``rejected`` with the precise category
2. no ``runs/<id>/out`` output survives (rollback) and nothing was written
   outside the service home (the canary tree is byte-identical)
3. the audit log records the same category, correlated to the run id
"""

from __future__ import annotations

import stat

import pytest

from archguard.errors import RejectionCategory

from fixtures_archive import (
    ZipSpec,
    build_tar,
    build_zip,
    build_zip_with_unix,
    tar_truncated_payload,
    zip_declared_size_mismatch,
    zip_with_bad_crc,
    zip_with_duplicate_name,
)


def assert_rejected(verdict, category, svc):
    assert verdict.status == "rejected", verdict.detail
    assert verdict.accepted is False
    assert verdict.category == category.value, verdict.detail
    assert verdict.detail  # never an empty explanation
    # Unknown states must never masquerade as success.
    assert verdict.status != "accepted"
    # A rejected run leaves no materialized output directory.
    run_dirs = list((svc["home"] / "runs").iterdir())
    assert run_dirs == [], f"rollback left {run_dirs}"
    # The staged upload bytes are also removed; only audit/db rows (which live
    # in audit/ and runs.db, not spool/) remain as evidence.
    spool_files = list((svc["home"] / "spool").iterdir())
    assert spool_files == [], f"rejected run left spool files {spool_files}"
    # Audit row is correlated and carries the same classification.
    row = svc["store"].get_run(verdict.run_id)
    assert row is not None
    assert row["status"] == "rejected"
    assert row["category"] == category.value
    events = svc["store"].get_events(verdict.run_id)
    assert all(e["run_id"] == verdict.run_id for e in events)
    reject = [e for e in events if e["name"] == "rejected"]
    assert len(reject) == 1
    assert reject[0]["detail"]["category"] == category.value


# --------------------------------------------------------------------------- #
# Path traversal (zip + tar), absolute paths, drive letters
# --------------------------------------------------------------------------- #

TRAVERSAL_ZIPS = [
    pytest.param(build_zip([ZipSpec("../evil.txt", data=b"x")]), id="zip-dotdot"),
    pytest.param(build_zip([ZipSpec("a/../../evil", data=b"x")]), id="zip-nested-dotdot"),
    pytest.param(build_zip([ZipSpec("/tmp/abs", data=b"x")]), id="zip-absolute"),
    pytest.param(build_zip([ZipSpec("C:/Windows/x", data=b"x")]), id="zip-drive"),
    pytest.param(build_zip([ZipSpec("a/../b/../../escape", data=b"x")]), id="zip-mixed"),
]


@pytest.mark.parametrize("data", TRAVERSAL_ZIPS)
def test_zip_path_traversal_rejected(svc, canary, data):
    before = canary["snapshot"]()
    v = svc["engine"].inspect(data, input_name="evil.zip")
    assert_rejected(v, RejectionCategory.PATH_TRAVERSAL, svc)
    assert canary["snapshot"]() == before


def test_tar_path_traversal_rejected(svc, canary):
    data = build_tar([("file", "../tar-evil", b"x")])
    before = canary["snapshot"]()
    v = svc["engine"].inspect(data, input_name="evil.tar")
    assert_rejected(v, RejectionCategory.PATH_TRAVERSAL, svc)
    assert canary["snapshot"]() == before


def test_tar_absolute_path_rejected(svc, canary):
    data = build_tar([("file", "/tmp/abs-evil", b"x")])
    v = svc["engine"].inspect(data, input_name="abs.tar")
    assert_rejected(v, RejectionCategory.PATH_TRAVERSAL, svc)


# --------------------------------------------------------------------------- #
# Same-name overwrite / case collision
# --------------------------------------------------------------------------- #

def test_exact_duplicate_rejected(svc, canary):
    from archguard.errors import RejectionCategory

    data = zip_with_duplicate_name(b"first", b"second")
    before = canary["snapshot"]()
    v = svc["engine"].inspect(data, input_name="dup.zip")
    assert_rejected(v, RejectionCategory.DUPLICATE_ENTRY, svc)
    assert canary["snapshot"]() == before


def test_case_collision_rejected(svc, canary):
    from archguard.errors import RejectionCategory

    data = build_zip([
        ZipSpec("Report.TXT", data=b"a"),
        ZipSpec("report.txt", data=b"b"),
    ])
    before = canary["snapshot"]()
    v = svc["engine"].inspect(data, input_name="case.zip")
    assert_rejected(v, RejectionCategory.CASE_COLLISION, svc)
    assert canary["snapshot"]() == before


# --------------------------------------------------------------------------- #
# Link chains
# --------------------------------------------------------------------------- #

def test_symlink_escape_rejected(svc, canary):
    from archguard.errors import RejectionCategory

    data = build_zip([ZipSpec("l", kind="symlink", target=b"../../../etc/passwd")])
    before = canary["snapshot"]()
    v = svc["engine"].inspect(data, input_name="link.zip")
    assert_rejected(v, RejectionCategory.SYMLINK_ESCAPE, svc)
    assert canary["snapshot"]() == before


def test_symlink_loop_rejected(svc, canary):
    from archguard.errors import RejectionCategory

    data = build_zip([
        ZipSpec("x", kind="symlink", target=b"y"),
        ZipSpec("y", kind="symlink", target=b"x"),
    ])
    before = canary["snapshot"]()
    v = svc["engine"].inspect(data, input_name="loop.zip")
    assert_rejected(v, RejectionCategory.SYMLINK_LOOP, svc)
    assert canary["snapshot"]() == before


def test_symlink_dangling_rejected(svc, canary):
    from archguard.errors import RejectionCategory

    data = build_zip([ZipSpec("l", kind="symlink", target=b"missing-file")])
    before = canary["snapshot"]()
    v = svc["engine"].inspect(data, input_name="dangle.zip")
    assert_rejected(v, RejectionCategory.SYMLINK_DANGLING, svc)
    assert canary["snapshot"]() == before


def test_dir_symlink_escape_rejected(svc, canary):
    # A directory-style link pointing out of root, with a child declared under
    # it: must be refused (classic "symlink + following member" escape).
    from archguard.errors import RejectionCategory

    data = build_zip([
        ZipSpec("dir", kind="symlink", target=b"../../.."),
        ZipSpec("dir/pwn.txt", data=b"pwn"),
    ])
    before = canary["snapshot"]()
    v = svc["engine"].inspect(data, input_name="direscape.zip")
    assert v.category in (
        RejectionCategory.SYMLINK_ESCAPE.value,
        RejectionCategory.SYMLINK_DANGLING.value,
    )
    assert v.status == "rejected"
    assert list((svc["home"] / "runs").iterdir()) == []
    assert canary["snapshot"]() == before


# --------------------------------------------------------------------------- #
# Hard links and special files
# --------------------------------------------------------------------------- #

def test_hardlink_rejected(svc, canary):
    from archguard.errors import RejectionCategory

    data = build_tar([
        ("file", "real.txt", b"hi"),
        ("hardlink", "hl", "real.txt"),
    ])
    before = canary["snapshot"]()
    v = svc["engine"].inspect(data, input_name="hl.tar")
    assert_rejected(v, RejectionCategory.HARDLINK, svc)
    assert canary["snapshot"]() == before


@pytest.mark.parametrize("kind,cat", [
    ("fifo", "ENTRY_SPECIAL"),
    ("chr", "ENTRY_SPECIAL"),
])
def test_special_file_rejected(svc, canary, kind, cat):
    from archguard.errors import RejectionCategory

    data = build_tar([(kind, "spooky")])
    before = canary["snapshot"]()
    v = svc["engine"].inspect(data, input_name=kind + ".tar")
    assert_rejected(v, RejectionCategory.ENTRY_SPECIAL, svc)
    assert canary["snapshot"]() == before


def test_zip_fifo_mode_rejected(svc, canary):
    from archguard.errors import RejectionCategory

    data = build_zip_with_unix("f", stat.S_IFIFO | 0o644)
    before = canary["snapshot"]()
    v = svc["engine"].inspect(data, input_name="fifo.zip")
    assert_rejected(v, RejectionCategory.ENTRY_SPECIAL, svc)
    assert canary["snapshot"]() == before


# --------------------------------------------------------------------------- #
# Declared length mismatches / checksum
# --------------------------------------------------------------------------- #

def test_zip_declared_size_mismatch_rejected(svc, canary):
    from archguard.errors import RejectionCategory

    data = zip_declared_size_mismatch()
    before = canary["snapshot"]()
    v = svc["engine"].inspect(data, input_name="size.zip")
    assert_rejected(v, RejectionCategory.DECLARED_SIZE_MISMATCH, svc)
    assert v.entry == "a.txt"
    assert canary["snapshot"]() == before


def test_zip_crc_mismatch_rejected(svc, canary):
    from archguard.errors import RejectionCategory

    data = zip_with_bad_crc()
    before = canary["snapshot"]()
    v = svc["engine"].inspect(data, input_name="crc.zip")
    assert_rejected(v, RejectionCategory.CONTENT_CRC_MISMATCH, svc)
    assert canary["snapshot"]() == before


def test_tar_declared_size_mismatch_rejected(svc, canary):
    from archguard.errors import RejectionCategory

    data = tar_truncated_payload()
    before = canary["snapshot"]()
    v = svc["engine"].inspect(data, input_name="short.tar")
    assert_rejected(v, RejectionCategory.DECLARED_SIZE_MISMATCH, svc)
    assert canary["snapshot"]() == before


# --------------------------------------------------------------------------- #
# Format
# --------------------------------------------------------------------------- #

def test_unknown_format_rejected(svc, canary):
    from archguard.errors import RejectionCategory

    before = canary["snapshot"]()
    v = svc["engine"].inspect(b"plain text, not an archive", input_name="x.dat")
    assert_rejected(v, RejectionCategory.FORMAT_UNSUPPORTED, svc)
    assert canary["snapshot"]() == before
