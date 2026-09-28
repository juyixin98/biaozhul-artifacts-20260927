"""Exhaustiveness of the restricted-pattern region enumeration.

We do not merely check that enumerate_regions returns strings; we prove, against
an independent brute-force matcher over a large brute universe, that every
membership vector observed anywhere also appears among the representatives.
"""

from __future__ import annotations

import itertools

from osdiff.patterns import GlobPattern, PatternError, enumerate_regions

from .oracle import _pat_match


def _vectors(patterns, strings):
    return {tuple(_pat_match(p, s) for p in patterns) for s in strings}


def test_exact_and_prefix_boundaries_distinguish_membership():
    patterns = ["photos/2026/", "photos/2026/private/*", "billing/*"]
    globs = [GlobPattern.parse(p) for p in patterns]
    reps = enumerate_regions(globs)

    # Exact prefix points are always representatives.
    assert "photos/2026/" in reps
    assert "photos/2026/private/" in reps
    assert "billing/" in reps

    # For boundary strings just inside/outside nested prefixes, the enumeration
    # guarantees an *equivalent representative* (identical membership vector), not
    # the literal string itself. Verify that guarantee directly.
    rep_vectors = {tuple(_pat_match(p, r) for p in patterns) for r in reps}
    boundary_strings = [
        "photos/2026/x",           # inside outer prefix, outside nested private
        "photos/2026/private/x",    # inside nested prefix
        "billing/2026/x",           # inside second prefix
        "photos/2025/x",            # outside everything
        "photos/2026/private",      # prefix proper minus trailing slash
    ]
    for s in boundary_strings:
        v = tuple(_pat_match(p, s) for p in patterns)
        assert v in rep_vectors, f"boundary {s!r} vector {v} has no representative"

    # Exact-only pattern "photos/2026/" must not match a continuation.
    exact = GlobPattern.parse("photos/2026/")
    assert exact.matches("photos/2026/") is True
    assert exact.matches("photos/2026/x") is False


def test_enumeration_covers_every_vector_in_brute_universe():
    patterns = ["a", "ab*", "ac/", "b*"]
    globs = [GlobPattern.parse(p) for p in patterns]
    reps = enumerate_regions(globs)

    alphabet = ["a", "b", "c", "/", "x", "\x01"]
    brute = [""] + ["".join(t) for n in range(1, 5) for t in itertools.product(alphabet, repeat=n)]

    rep_vectors = _vectors(patterns, reps)
    for s in brute:
        v = tuple(_pat_match(p, s) for p in patterns)
        assert v in rep_vectors, f"string {s!r} has vector {v} with no representative"


def test_single_trailing_star_and_literal_only_are_accepted():
    assert GlobPattern.parse("x*").matches("xyz") is True
    assert GlobPattern.parse("x*").matches("x") is True
    assert GlobPattern.parse("x").matches("x") is True
    assert GlobPattern.parse("x").matches("xy") is False


def test_embedded_wildcards_are_refused_not_guessed():
    for bad in ["a/*/b", "a**", "**", "a/b*c"]:
        try:
            GlobPattern.parse(bad)
        except PatternError:
            continue
        raise AssertionError(f"{bad!r} must be rejected")


def test_empty_prefix_star_covers_everything():
    reps = enumerate_regions([GlobPattern.parse("*")])
    assert reps, "universal prefix must still yield representatives"
    assert all(_pat_match("*", r) for r in reps)
