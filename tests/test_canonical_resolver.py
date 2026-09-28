"""Direct unit tests for the pure path normalization and symlink resolver.

These lock down the lexical semantics independently of any archive format,
including POSIX ``..``-through-symlink edge cases that naive normalizers miss.
"""
from __future__ import annotations

import pytest

from app.config import Budgets
from app.kernel.canonical import normalize_name, resolve, split_dirname
from app.kernel.errors import PathEscape, SymlinkEscape, SymlinkLoop, UnsafeName

B = Budgets()


def test_normalize_basic_and_traversal():
    assert normalize_name("a/b/c.txt") == ("a", "b", "c.txt")
    assert normalize_name("./a/./b") == ("a", "b")
    assert normalize_name("a//b") == ("a", "b")
    assert normalize_name("a/../b") == ("b",)


@pytest.mark.parametrize("bad", [
    "../x", "a/../../x", "..", "/abs/path", "C:/win", "\\\\share\\x",
    "a/../..", "a/../../b",
])
def test_normalize_rejects_escape(bad):
    with pytest.raises((PathEscape, UnsafeName)):
        normalize_name(bad)


@pytest.mark.parametrize("bad", ["a\x00b", "a\nb", "a\\b"])
def test_normalize_rejects_control_and_backslash(bad):
    with pytest.raises((UnsafeName, PathEscape)):
        normalize_name(bad)


def test_split_dirname_drops_trailing_slash():
    assert split_dirname("a/b/") == ("a", "b")


def test_resolve_no_links():
    assert resolve(("a", "b"), {}, steps_budget=40, evidence_name="t") == ("a", "b")


def test_resolve_simple_link():
    links = {("a",): ("real",)}
    # a/file -> real/file
    assert resolve(("a", "file"), links, steps_budget=40, evidence_name="t") == ("real", "file")


def test_resolve_relative_link_with_dotdot_posix():
    # a/dir -> ../store ; a/dir/one.txt must resolve to store/one.txt
    links = {("a", "dir"): ("..", "store")}
    got = resolve(("a", "dir", "one.txt"), links, steps_budget=40, evidence_name="t")
    assert got == ("store", "one.txt")


def test_resolve_dotdot_through_link_pops_parent():
    # root-level "dir" -> ../store is an escape because it pops the root.
    links = {("dir",): ("..", "store")}
    with pytest.raises(SymlinkEscape):
        resolve(("dir", "x"), links, steps_budget=40, evidence_name="dir/x")


def test_resolve_link_chain():
    # a -> b, b -> c ; a/f -> c/f
    links = {("a",): ("b",), ("b",): ("c",)}
    assert resolve(("a", "f"), links, steps_budget=40, evidence_name="t") == ("c", "f")


def test_resolve_detects_loop():
    links = {("a",): ("b",), ("b",): ("a",)}
    with pytest.raises(SymlinkLoop):
        resolve(("a", "f"), links, steps_budget=40, evidence_name="a/f")


def test_resolve_self_loop():
    links = {("x",): ("x",)}
    with pytest.raises(SymlinkLoop):
        resolve(("x",), links, steps_budget=40, evidence_name="x")


def test_resolve_step_budget():
    # Chain that bounces many times before looping must be bounded.
    links = {("a",): ("b", "..", "a")}
    with pytest.raises(SymlinkLoop):
        resolve(("a",), links, steps_budget=10, evidence_name="a")


def test_resolve_dotdot_after_link_is_relative_to_resolved():
    # l -> sub/target ; l/../sibling = sub/sibling (POSIX), NOT root/sibling.
    links = {("l",): ("sub", "target")}
    got = resolve(("l", "..", "sibling"), links, steps_budget=40, evidence_name="t")
    assert got == ("sub", "sibling")
