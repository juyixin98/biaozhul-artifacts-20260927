"""Unit tests for name normalization and the canonical path graph."""

from __future__ import annotations

import pytest

from archguard.archiveio import Entry, EntryKind
from archguard.budget import Budget
from archguard.errors import RejectionCategory, RejectionError
from archguard.paths import Planner, normalize_link_target, normalize_name


def budget(**over) -> Budget:
    base = {
        "max_total_bytes": 10_000,
        "max_files": 100,
        "max_depth": 4,
        "max_symlink_hops": 8,
        "max_compression_ratio": 1000.0,
    }
    base.update(over)
    return Budget(**base)


def file_entry(name: str, size: int = 1, index: int = 0) -> Entry:
    return Entry(name=name, kind=EntryKind.FILE, size=size, compressed_size=size,
                 index=index)


# --------------------------------------------------------------------------- #
# normalize_name
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw,parts",
    [
        ("a/b/c", ("a", "b", "c")),
        ("./a/./b/", ("a", "b")),
        ("a//b", ("a", "b")),
        ("dir/", ("dir",)),
        ("A/B.TXT", ("A", "B.TXT")),
    ],
)
def test_normalize_name_accepts(raw, parts):
    assert normalize_name(raw) == parts


@pytest.mark.parametrize(
    "raw,category",
    [
        ("../evil", RejectionCategory.PATH_TRAVERSAL),
        ("a/../../evil", RejectionCategory.PATH_TRAVERSAL),
        ("/etc/passwd", RejectionCategory.PATH_TRAVERSAL),
        ("C:/Windows/x", RejectionCategory.PATH_TRAVERSAL),
        ("a\\b", RejectionCategory.PATH_INVALID),
        ("a/b\x00c", RejectionCategory.PATH_INVALID),
        ("", RejectionCategory.ARCHIVE_CORRUPT),
    ],
)
def test_normalize_name_rejects(raw, category):
    with pytest.raises(RejectionError) as ei:
        normalize_name(raw)
    assert ei.value.category is category


# --------------------------------------------------------------------------- #
# normalize_link_target
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "target,link,expected",
    [
        ("b", ("a", "l"), ["a", "b"]),
        ("../b", ("a", "l"), ["b"]),
        ("x/../y", ("a", "l"), ["a", "y"]),
        ("../../ok", ("a", "b", "l"), ["ok"]),
    ],
)
def test_normalize_link_inside(target, link, expected):
    assert normalize_link_target(target, link) == expected


@pytest.mark.parametrize(
    "target",
    ["/abs/x", "../..", "a/../../../x", "C:/x", "bad\\x", "a\x00b"],
)
def test_normalize_link_escape(target):
    with pytest.raises(RejectionError) as ei:
        normalize_link_target(target, ("a", "l"))
    assert ei.value.category is RejectionCategory.SYMLINK_ESCAPE


# --------------------------------------------------------------------------- #
# Graph conflicts
# --------------------------------------------------------------------------- #

def test_case_collision_detected():
    p = Planner(budget())
    p.add(file_entry("Foo.txt", index=0))
    with pytest.raises(RejectionError) as ei:
        p.add(file_entry("foo.TXT", index=1))
    assert ei.value.category is RejectionCategory.CASE_COLLISION


def test_duplicate_detected():
    p = Planner(budget())
    p.add(file_entry("a.txt", index=0))
    with pytest.raises(RejectionError) as ei:
        p.add(file_entry("a.txt", index=1))
    assert ei.value.category is RejectionCategory.DUPLICATE_ENTRY


def test_file_then_child_conflict():
    p = Planner(budget())
    p.add(file_entry("a", index=0))
    with pytest.raises(RejectionError) as ei:
        p.add(file_entry("a/b", index=1))
    assert ei.value.category is RejectionCategory.PATH_CONFLICT


def test_implicit_dir_then_file_restatement_conflict():
    # "a/b" forces implicit dir "a"; later declaring "a" as a file conflicts.
    p = Planner(budget())
    p.add(file_entry("a/b", index=0))
    with pytest.raises(RejectionError) as ei:
        p.add(file_entry("a", index=1))
    assert ei.value.category is RejectionCategory.PATH_CONFLICT


def test_implicit_parent_dir_case_collision():
    p = Planner(budget())
    p.add(file_entry("Dir/x", index=0))
    with pytest.raises(RejectionError) as ei:
        p.add(file_entry("dir/y", index=1))
    assert ei.value.category is RejectionCategory.CASE_COLLISION


def test_depth_budget_enforced():
    p = budget(max_depth=2) and Planner(budget(max_depth=2))
    with pytest.raises(RejectionError) as ei:
        p.add(file_entry("a/b/c", index=0))
    assert ei.value.category is RejectionCategory.BUDGET_DEPTH


def test_implicit_dirs_created():
    p = Planner(budget())
    p.add(file_entry("x/y/z.txt", index=0))
    assert ("x",) in p.plan.nodes
    assert ("x", "y") in p.plan.nodes
    assert p.plan.nodes[("x",)].implicit is True


def test_symlink_self_loop():
    p = Planner(budget())
    link = Entry(name="l", kind=EntryKind.SYMLINK, size=1, compressed_size=1, index=0)
    p.add(link, "l")
    with pytest.raises(RejectionError) as ei:
        p.resolve_symlinks()
    assert ei.value.category is RejectionCategory.SYMLINK_LOOP


def test_dangling_link_rejected():
    p = Planner(budget())
    link = Entry(name="l", kind=EntryKind.SYMLINK, size=7, compressed_size=7, index=0)
    p.add(link, "ghost")
    with pytest.raises(RejectionError) as ei:
        p.resolve_symlinks()
    assert ei.value.category is RejectionCategory.SYMLINK_DANGLING
