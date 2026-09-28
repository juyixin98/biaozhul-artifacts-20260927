"""Policy tests: traversal, collisions, links, kinds, budgets.

Each test asserts the SPECIFIC failure category and proves that rejection
left no ``output/`` directory (and touched nothing outside the run dir).
Fixtures are built independently in tests/fixtures/builder.py.
"""
from __future__ import annotations

import gzip

import pytest

from tests.fixtures import builder as fb

# (label, fixture-bytes, expected failure category)
ZIP_POLICY_CASES = [
    ("relative traversal ../", lambda: fb.zip_bytes([{"name": "../evil.txt", "data": b"x"}]),
     "path_escape"),
    ("nested traversal", lambda: fb.zip_bytes([{"name": "a/../../evil", "data": b"x"}]),
     "path_escape"),
    ("absolute path", lambda: fb.zip_bytes([{"name": "/etc/evil", "data": b"x"}]),
     "path_escape"),
    ("same-name overwrite", lambda: fb.zip_bytes([{"name": "a", "data": b"1"},
                                                  {"name": "a", "data": b"2"}]),
     "duplicate_name"),
    ("case collision A/a", lambda: fb.zip_bytes([{"name": "A", "data": b"1"},
                                                  {"name": "a", "data": b"2"}]),
     "case_collision"),
    ("symlink absolute escape", lambda: fb.zip_bytes([{"name": "l", "link": "/etc/passwd"}]),
     "symlink_escape"),
    ("symlink parent escape", lambda: fb.zip_bytes([
        {"name": "l", "link": "../../../../etc/passwd"},
        {"name": "l/leaf", "data": b"y"}]),
     "symlink_escape"),
    ("symlink loop a->b->a", lambda: fb.zip_bytes([
        {"name": "a", "link": "b"}, {"name": "b", "link": "a"},
        {"name": "a/inside", "data": b"z"}]),
     "symlink_loop"),
]

TAR_POLICY_CASES = [
    ("tar traversal", lambda: fb.tar_bytes([{"name": "../../evil", "data": b"x"}]),
     "path_escape"),
    ("tar absolute", lambda: fb.tar_bytes([{"name": "/tmp/evil", "data": b"x"}]),
     "path_escape"),
    ("tar hardlink", lambda: fb.tar_bytes([
        {"name": "t", "data": b"x"}, {"name": "h", "hardlink": "t"}]),
     "hardlink_rejected"),
    ("tar fifo", lambda: fb.tar_bytes([{"name": "p", "fifo": True}]),
     "special_file_rejected"),
    ("tar symlink escape", lambda: fb.tar_bytes([
        {"name": "l", "link": "../../../etc"}, {"name": "l/x", "data": b"y"}]),
     "symlink_escape"),
]

FORMAT_CASES = [
    ("encrypted zip", lambda: fb.zip_encryption_flag(), "unsupported_encryption"),
    ("deflate64 method", lambda: fb.zip_unsupported_method(), "unsupported_compression"),
    ("gzip tar", lambda: gzip.compress(fb.tar_bytes([{"name": "a", "data": b"x"}])),
     "unsupported_compression"),
    ("garbage", lambda: b"not an archive body " * 10, "unsupported_format"),
    ("truncated zip", lambda: fb.zip_truncated(), "corrupt_archive"),
    ("truncated tar", lambda: fb.tar_with_truncated_payload(), "corrupt_archive"),
]


def _assert_rejected_isolated(result, settings, expected_category):
    assert result.verdict == "rejected", result.verdict
    assert result.error.category == expected_category, (
        result.error.category, result.error.message
    )
    # No extraction output may exist after rejection.
    assert not result.workspace.output_dir.exists(), "output dir survived rejection"
    # Everything the run created lives strictly under its own run root.
    runs_root = settings.workspace_root / "runs"
    for child in runs_root.rglob("*"):
        assert str(child).startswith(str(result.workspace.root)), child


@pytest.mark.parametrize("label,make,category", ZIP_POLICY_CASES)
def test_zip_policy_rejections(service, settings, label, make, category):
    result = service.extract(make(), "evil.zip")
    _assert_rejected_isolated(result, settings, category)


@pytest.mark.parametrize("label,make,category", TAR_POLICY_CASES)
def test_tar_policy_rejections(service, settings, label, make, category):
    result = service.extract(make(), "evil.tar")
    _assert_rejected_isolated(result, settings, category)


@pytest.mark.parametrize("label,make,category", FORMAT_CASES)
def test_format_rejections(service, settings, label, make, category):
    result = service.extract(make(), "evil.bin")
    _assert_rejected_isolated(result, settings, category)


def test_entry_count_budget(service, settings):
    data = fb.zip_bytes([{"name": f"f{i}", "data": b"x"} for i in range(60)])
    result = service.extract(data, "many.zip")
    _assert_rejected_isolated(result, settings, "budget_entry_count_exceeded")


def test_depth_budget(service, settings):
    data = fb.zip_bytes([{"name": "/".join(["d"] * 10) + "/f", "data": b"x"}])
    result = service.extract(data, "deep.zip")
    _assert_rejected_isolated(result, settings, "budget_depth_exceeded")


def test_per_file_size_budget(service, settings):
    result = service.extract(fb.zip_stored_bomb_headers(), "bomb.zip")
    _assert_rejected_isolated(result, settings, "budget_file_size_exceeded")


def test_declared_length_lie_is_exact_category(service, settings):
    # Must NOT be mis-classified as a compression bomb.
    result = service.extract(fb.zip_with_declared_size_mismatch(), "lie.zip")
    assert result.error.category == "declared_length_mismatch"
    assert not result.workspace.output_dir.exists()


def test_crc_mismatch_is_integrity_failure(service, settings):
    result = service.extract(fb.zip_with_crc_mismatch(), "crc.zip")
    assert result.verdict == "rejected"
    assert result.error.category == "integrity_failure"
    assert not result.workspace.output_dir.exists()


def test_genuine_deflate_bomb(service, settings):
    # Repetitive 240 KiB payload deflates to a few hundred bytes (~900x).
    payload = fb.random_bytes(4, seed=9) * 60000
    # Total/per-file budgets in conftest are 64/32 KiB; the bomb exceeds those
    # too. Build a service with generous size budgets to isolate the RATIO rule.
    from app.config import Budgets, Policy, Settings
    from app.audit.audit import AuditDB
    from app.service import GuardService

    custom = Settings(
        version="t", workspace_root=settings.workspace_root,
        audit_db=settings.audit_db.parent / "bomb.db",
        max_upload_bytes=10 * 1024 * 1024,
        budgets=Budgets(
            max_total_uncompressed_bytes=200 * 1024 * 1024,
            max_file_size_bytes=200 * 1024 * 1024,
            max_entries=50, max_depth=8, max_compression_ratio=50,
            symlink_resolution_steps=40),
        policy=Policy())
    db = AuditDB(custom.audit_db, version="t")
    try:
        bomb_service = GuardService(custom, db)
        result = bomb_service.extract(fb.zip_bytes([{"name": "z", "data": payload}]), "b.zip")
        assert result.error.category == "compression_bomb"
        assert not result.workspace.output_dir.exists()
    finally:
        db.close()


def test_nothing_written_outside_run_root_on_rejection(service, settings, sentinel_outside):
    canary = sentinel_outside / "canary.txt"
    before = canary.read_bytes()
    service.extract(fb.zip_bytes([{"name": "../escape", "data": b"x"}]), "e.zip")
    assert canary.read_bytes() == before
    # The escape target must not exist anywhere outside the run directory.
    escape_target = settings.workspace_root.parent / "escape"
    assert not escape_target.exists()
